"""Transport-agnostic single-query submit pipeline.

Extracted verbatim from the Slack modal handler so the web API and the
Slack modal share ONE implementation of the security-critical path:

    validate_submission()  kill switch, rate limit, static + AST safety,
                           grant + database-scope + tier checks,
                           justification rules, pre-flight EXPLAIN,
                           schedule resolution, duplicate guard
    create_request()       auto-approve resolution (grant, schedule
                           re-evaluation, fingerprint cache), the INSERT
                           (pending or auto-approved), audit rows, and
                           supersede-on-edit — one transaction
    dispatch_and_notify()  admin FYI / approval fan-out, executor
                           dispatch, optional requester DMs

Rejections carry transport-neutral field names; the Slack handler maps
them to modal block ids, the web API maps them to HTTP error payloads.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import admins, ast_safety, audit, auto_approve, db, pre_flight
from . import config as cfg
from . import profile_sync, query_safety, requesters, targets, teams

log = logging.getLogger(__name__)

# Column list returned by request INSERT/UPDATE ... RETURNING across the
# submit + decision paths. Shared with the Slack handlers.
REQUEST_RETURNING = (
    "id, requester_slack_id, requester_name, target_server_id, "
    "database_name, query, wants_result, result_format, justification, status, "
    "decided_by_slack_id, decided_by_name, decided_at, scheduled_for, "
    "requester_dm_channel_id, requester_dm_message_ts, "
    "bundle_id, position, risk_summary, origin, engine, required_tier"
)


# ---- Kill-switch: bot_config-driven master toggle to halt new traffic ----

def kill_switch_on() -> bool:
    val = (cfg.get_setting("kill_switch", "off") or "off").strip().lower()
    return val in {"on", "true", "yes", "1"}


def kill_switch_message() -> str:
    return cfg.get_setting(
        "kill_switch_message",
        ":construction: The SQL bot is temporarily disabled. Please try again "
        "later or contact the DBA team.",
    )


# ---- Pipeline dataclasses -------------------------------------------------

@dataclass(frozen=True)
class Rejection:
    """A validation failure. `field` is transport-neutral:
    kill_switch | rate_limit | query | server | database | justification |
    schedule_date | schedule_time. `reason` disambiguates same-field
    failures for HTTP status mapping (tier_exceeds, duplicate)."""
    field: str
    message: str
    reason: str | None = None


@dataclass
class Prepared:
    """A validated submission, ready for create_request()."""
    user_id: str
    user_name: str | None
    target: "targets.TargetServer"
    database: str
    query: str
    required_mode: str
    justification: str | None
    wants_result: bool
    result_format: str
    sched_for: datetime | None
    explain_plan: list | None
    risk_summary: str | None
    origin: str = "slack"
    client_ip: str | None = None
    user_agent: str | None = None


@dataclass
class Outcome:
    """What create_request() did."""
    row: dict
    auto_approved: bool
    aa_grant: dict | None = None
    fp_hit: dict | None = None
    aa_warn: str | None = None
    supersedes_id: int | None = None
    superseded_row: dict | None = None


# ---- Step 1: validation ---------------------------------------------------

def needs_justification(required_mode: str, will_auto_approve: bool) -> bool:
    """Will the submit path demand a justification for this request?

    The single authority for that question. `POST /classify` publishes the answer
    so the editor can render the field only when it is actually needed, and the
    editor must not have its own copy of the rule: it had one, and it was wrong
    in two directions at once — DDL-only when RW needs one too, and blind to
    auto-approval.

    `will_auto_approve` must be the SUBMIT-TIME verdict. A scheduled request is
    never exempt (its grant may lapse before the run time, which puts a human
    back in the loop), so callers pass False for the scheduled case.
    """
    if will_auto_approve:
        return False
    if required_mode != "ro":
        return True
    return cfg.get_bool("require_justification", False)


def validate_submission(
    user_id: str,
    user_name: str | None,
    *,
    target_server_id: int,
    database_name: str | None,
    query: str,
    justification: str | None,
    wants_result: bool = True,
    result_format: str = "csv",
    schedule_date: str | None = None,
    schedule_time: str | None = None,
    origin: str = "slack",
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> Prepared | Rejection:
    """Run every submit-time check in the exact order the Slack modal
    always has. Returns a Prepared on success, a Rejection on the first
    failure."""
    if kill_switch_on():
        return Rejection("kill_switch", kill_switch_message())

    # Graceful restart: while this process is draining for a restart it accepts
    # no new work (in-flight queries are allowed to finish first). Process-local.
    from . import lifecycle
    if lifecycle.is_draining():
        return Rejection(
            "draining",
            "QueryHub is restarting for maintenance — please resubmit in a "
            "moment.")

    # Per-user rate limit: cap in-flight requests (pending / approved /
    # scheduled / executing) per Slack user. Admins are exempt — they may
    # be triaging on behalf of others. Count is a coarse anti-flood gate,
    # not a fairness scheduler.
    if not admins.is_admin(user_id):
        max_open = cfg.get_int("max_open_requests_per_user", 5)
        open_count = requesters.open_request_count(user_id)
        if open_count >= max_open:
            return Rejection(
                "rate_limit",
                f"You already have {open_count} request(s) in flight "
                f"(max {max_open}). Wait for them to complete, or ask "
                f"an admin to cancel some.")

    query = (query or "").strip()
    if not query:
        return Rejection("query", "Provide your SQL query.")

    min_len = cfg.get_int("min_query_length", 6)
    if len(query) < min_len:
        return Rejection(
            "query", f"Query must be at least {min_len} characters.")

    # Resolve the target first so the safety pass uses its engine: a
    # non-Postgres engine parses with its own sqlglot dialect + dangerous-
    # function blocklist, and a read-only engine rejects writes up front.
    target = targets.get(target_server_id)
    if target is None:
        return Rejection("server", "Selected server is no longer available.")

    safety = query_safety.analyze(query, engine=target.engine)
    if safety.blocked:
        return Rejection("query", " ".join(safety.blockers)[:3000])

    # Mode required by the query: ro / rw / ddl
    required_mode = query_safety.required_mode(query, engine=target.engine)

    # Effective grant: user_target_grants overrides team_target_grants;
    # admins / bypass requesters get a synthetic ddl-on-everything grant.
    grant = teams.effective_grant_for_user(user_id, target_server_id)
    if grant is None:
        return Rejection("server", "You are not authorized to query this server.")

    database = database_name or target.default_database

    # allowed_databases is None = no restriction; non-None set = whitelist.
    allowed_dbs = grant["allowed_databases"]
    if allowed_dbs is not None and database not in allowed_dbs:
        sample = ", ".join(f"`{d}`" for d in sorted(allowed_dbs)[:8])
        more = "" if len(allowed_dbs) <= 8 else f" (+{len(allowed_dbs) - 8} more)"
        return Rejection(
            "database",
            f"You don't have access to database `{database}` on this "
            f"server. Allowed: {sample}{more}.")

    # Mode permission check (ro < rw < ddl). Resolve the tier for THIS specific
    # database — not grant["mode"], which is the max tier across every grant on
    # the target and would let a RW-on-dbA grant authorize a write to a
    # RO-only dbB (cross-product escalation). Fail closed if no
    # grant covers this database (the allowed-databases check above should have
    # already caught that, but never trust the union here).
    rank = {"ro": 0, "rw": 1, "ddl": 2}
    granted_mode = teams.effective_mode_for_database(
        user_id, target_server_id, database)
    if granted_mode is None or rank[required_mode] > rank[granted_mode]:
        if required_mode == "rw":
            msg = (f"You don't have *write* access on `{target.alias}`. "
                   f"This is a read-only grant — contact your team lead or "
                   f"the DBA team to request RW.")
        else:  # ddl
            msg = (f"You don't have *DDL* access on `{target.alias}` "
                   f"(CREATE/ALTER/DROP/TRUNCATE/VACUUM/...). DDL grants are "
                   f"issued individually by the DBA team.")
        return Rejection("query", msg, reason="tier_exceeds")

    # Mandatory justification for write/DDL queries (audit hygiene) — unless the
    # request is going to be auto-approved anyway.
    #
    # The field has two readers. The first is the approver, deciding; an
    # auto-approved request has no approver, so requiring prose there asks for
    # something nobody reads at decision time. The second reader is whoever opens
    # the audit log later asking why a write ran, and that one still gets an
    # answer: an auto-approved submission records `grant_id`, and the grant
    # carries its own `reason`, written by the admin who issued it. So the intent
    # is recorded once, by the person best placed to state it, instead of
    # re-typed per query by someone who has already been exempted from review.
    #
    # Resolved here with its own lookup rather than reusing the one the caller
    # does, because that happens ~300 lines below — after the pre-flight EXPLAIN
    # and the rate-limit and duplicate checks. A second indexed read is cheaper
    # than moving this rejection past all of them and changing which error a user
    # meets first.
    #
    # Fail closed on both counts. A SCHEDULED request is never exempt: its grant
    # may expire before the run time, in which case the caller falls back to
    # normal approval and the reason is needed after all. And any error resolving
    # the grant leaves the requirement in place.
    justification = (justification or "").strip() or None
    auto_approve_exempt = False
    if not justification and not (schedule_date or schedule_time):
        try:
            auto_approve_exempt = (
                admins.is_super_admin(user_id)
                or auto_approve.effective_grant(
                    user_id, required_mode,
                    target_server_id=target_server_id,
                    database_name=database) is not None)
        except Exception:
            log.exception(
                "auto-approve lookup failed while deciding whether a "
                "justification is required for %s on target %s/%s — requiring "
                "one", user_id, target_server_id, database)
            auto_approve_exempt = False

    if not justification and needs_justification(required_mode, auto_approve_exempt):
        return Rejection(
            "justification",
            f"Justification is required for {required_mode.upper()} queries."
            if required_mode != "ro" else "Justification is required.")

    # Pre-flight EXPLAIN: parse + plan against the target without executing.
    # RO ONLY — see pre_flight.py for why RW/DDL are never EXPLAIN'd here.
    # For RW we still derive an affected-rows estimate (EXPLAIN-only in a
    # READ ONLY txn, fail-open).
    explain_plan: list | None = None
    risk_summary: str | None = None
    if (pre_flight.is_enabled() and required_mode == "ro"
            and pre_flight.is_explainable(query)):
        ok, err, plan = pre_flight.explain(
            target.id, database, required_mode, query,
        )
        if not ok:
            return Rejection(
                "query", f"Query failed validation: {err}"[:3000])
        if plan is not None:
            # Risk hint for the admin DM — always derived (cheap), even
            # when full-plan logging is off.
            try:
                risk_summary = pre_flight.risk_summary_text(plan)
            except Exception:
                log.exception("risk_summary_text failed; continuing without hint")
            if pre_flight.is_plan_logging_enabled():
                # Cap stored plan at 64KB. EXPLAIN of huge joins can produce
                # multi-MB JSON; storing it unbounded is a slow disk-fill DoS
                # against the metadata DB.
                serialized = json.dumps(plan)
                if len(serialized) > 65536:
                    explain_plan = [{
                        "_truncated": True,
                        "_original_size_bytes": len(serialized),
                        "_note": "EXPLAIN plan exceeded 64KB cap; not stored.",
                    }]
                else:
                    explain_plan = plan
    elif pre_flight.is_enabled() and required_mode == "rw":
        try:
            risk_summary = pre_flight.explain_write_estimate(
                target.id, database, required_mode, query,
            )
        except Exception:
            log.exception("write-estimate failed; continuing without hint")

    # Optional scheduling: both date and time must be supplied together.
    # Picker values are wall-clock in the user's local timezone (from the
    # Slack profile); stored as UTC. Unknown tz falls back to UTC.
    sched_for: datetime | None = None
    if schedule_date or schedule_time:
        if not (schedule_date and schedule_time):
            which = "schedule_date" if not schedule_date else "schedule_time"
            return Rejection(
                which, "Provide both date and time, or leave both empty.")
        user_tz_name = profile_sync.lookup_tz(user_id) or "UTC"
        try:
            user_tz = ZoneInfo(user_tz_name)
        except ZoneInfoNotFoundError:
            user_tz = timezone.utc
        try:
            local_dt = datetime.fromisoformat(
                f"{schedule_date}T{schedule_time}:00").replace(tzinfo=user_tz)
            sched_for = local_dt.astimezone(timezone.utc)
        except ValueError:
            return Rejection("schedule_date", "Invalid date or time.")
        max_days = cfg.get_int("max_schedule_days", 7)
        if max_days <= 0:
            return Rejection(
                "schedule_date",
                "Scheduling is disabled. Leave date and time empty.")
        now = datetime.now(timezone.utc)
        if sched_for <= now:
            return Rejection(
                "schedule_time", "Scheduled time must be in the future (UTC).")
        if sched_for > now + timedelta(days=max_days):
            return Rejection(
                "schedule_date",
                f"Scheduled time exceeds the {max_days}-day max.")

    # Duplicate guard: same (user, target, database, query) already in
    # flight? Block the re-submit so the user doesn't accidentally create
    # N copies of the same work.
    dup = db.fetch_one(
        "SELECT id, status FROM requests "
        "WHERE requester_slack_id = %s "
        "  AND target_server_id   = %s "
        "  AND database_name      = %s "
        "  AND query              = %s "
        "  AND status IN ('pending','changes_requested','approved',"
        "                 'scheduled','executing') "
        "ORDER BY id DESC LIMIT 1",
        (user_id, target_server_id, database, query),
    )
    if dup is not None:
        return Rejection(
            "query",
            f"You already have an active request "
            f"(#{dup['id']}, status={dup['status']}) with the same "
            f"query on this target+database. Wait for it to "
            f"complete, or cancel it before resubmitting.",
            reason="duplicate")

    return Prepared(
        user_id=user_id,
        user_name=user_name,
        target=target,
        database=database,
        query=query,
        required_mode=required_mode,
        justification=justification,
        wants_result=wants_result,
        result_format=result_format or "csv",
        sched_for=sched_for,
        explain_plan=explain_plan,
        risk_summary=risk_summary,
        origin=origin,
        client_ip=client_ip,
        user_agent=user_agent,
    )


# ---- Step 2: auto-approve resolution + INSERT (one transaction) -----------

def _recheck_open_limits(
    cur, prep: Prepared, supersedes_id: int | None
) -> Rejection | None:
    """Re-check the per-user rate limit and the duplicate guard using the
    create transaction's cursor. validate_submission() runs
    these as a separate read, so two submissions racing between that read and
    the INSERT would both pass — breaching the in-flight cap or creating
    duplicate work. Call this while holding a per-user advisory lock so the
    check and the INSERT are atomic for a given requester. An edited
    resubmission withdraws its original in the same transaction, so exclude it
    from both checks (mirrors it not counting against the user)."""
    exclude = () if supersedes_id is None else (supersedes_id,)
    extra = "" if supersedes_id is None else " AND id <> %s"

    if not admins.is_admin(prep.user_id):
        max_open = cfg.get_int("max_open_requests_per_user", 5)
        cur.execute(
            "SELECT count(*) AS n FROM requests "
            "WHERE requester_slack_id = %s "
            "  AND status IN ('pending','changes_requested','approved',"
            "                 'scheduled','executing')" + extra,
            (prep.user_id, *exclude),
        )
        open_count = cur.fetchone()["n"]
        if open_count >= max_open:
            return Rejection(
                "rate_limit",
                f"You already have {open_count} request(s) in flight "
                f"(max {max_open}). Wait for them to complete, or ask "
                f"an admin to cancel some.")

    cur.execute(
        "SELECT id, status FROM requests "
        "WHERE requester_slack_id = %s AND target_server_id = %s "
        "  AND database_name = %s AND query = %s "
        "  AND status IN ('pending','changes_requested','approved',"
        "                 'scheduled','executing')" + extra + " "
        "ORDER BY id DESC LIMIT 1",
        (prep.user_id, prep.target.id, prep.database, prep.query, *exclude),
    )
    dup = cur.fetchone()
    if dup is not None:
        return Rejection(
            "query",
            f"You already have an active request "
            f"(#{dup['id']}, status={dup['status']}) with the same "
            f"query on this target+database. Wait for it to "
            f"complete, or cancel it before resubmitting.",
            reason="duplicate")
    return None


def reserve_request_id(user_id: str) -> int:
    """Reserve the id a request WILL have, before it has any content.

    A query tab shows its number from the moment it opens, and that number has
    to be the real request id — an operator watching a slow query in a second
    window, or quoting it in chat, needs it before they submit. Handing out a
    separate "session id" and a request id later means two numbers for one
    thing.

    The row is a `draft`: no target, no database, no SQL (migration 086 relaxed
    `target_server_id` and added a CHECK that keeps every OTHER status complete).
    It is invisible to `requests_reportable`, and therefore to all 19 views that
    read requests through it — a tab somebody opened and closed is not work
    anybody submitted, and counting it would inflate every volume figure.

    Capped per user: this is reachable by anything holding a session, and a
    runaway loop should burn a bounded number of ids, not the sequence.
    """
    cap = max(1, cfg.get_int("draft_request_max_open", 50))
    with db.transaction() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM requests "
            " WHERE requester_slack_id = %s AND status = 'draft'", (user_id,))
        if (cur.fetchone() or {}).get("n", 0) >= cap:
            # Reap this user's oldest drafts rather than refusing: they are
            # abandoned tabs, and failing here would block opening a new one.
            cur.execute(
                "DELETE FROM requests WHERE id IN ("
                "  SELECT id FROM requests"
                "   WHERE requester_slack_id = %s AND status = 'draft'"
                "   ORDER BY created_at LIMIT GREATEST(1, %s / 2))",
                (user_id, cap))
        cur.execute(
            "INSERT INTO requests "
            "  (requester_slack_id, target_server_id, database_name, query, status) "
            "VALUES (%s, NULL, '', '', 'draft') RETURNING id", (user_id,))
        return cur.fetchone()["id"]


def reap_stale_drafts() -> int:
    """Delete reserved ids nobody submitted. Returns how many.

    Abandoned drafts are the normal case, not a fault: people open a tab, change
    their mind, close it. Nothing is lost by deleting one — a draft has no SQL, no
    target and no audit rows, and its id was only ever a promise.

    Runs from the scheduler loop rather than a cron entry so it also works in the
    vanilla profile, and because the partial index makes it a cheap look at just
    the drafts instead of the whole table.
    """
    hours = cfg.get_int("draft_request_ttl_hours", 24)
    if hours <= 0:
        return 0
    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM requests "
            " WHERE status = 'draft' "
            "   AND created_at < NOW() - make_interval(hours => %s)", (hours,))
        return cur.rowcount


def _claim_draft(cur, draft_id: int | None, user_id: str) -> int | None:
    """Delete the caller's reserved draft so its id can be reused by the real row.

    Returns the id to insert with, or None to let the sequence pick one.

    Deleting and re-inserting with the same id, rather than UPDATEing the draft
    into place, is deliberate: the two INSERTs below carry the rate-limit
    recheck, the audit rows and the auto-approve decision, and a third near-copy
    of that SQL as an UPDATE is how those three drift apart. A draft holds no
    information, so removing it loses nothing — the id was the whole point.

    Fail-open on purpose. A draft that expired, was reaped, or belongs to
    somebody else gives up the reserved number and the submission proceeds with
    a fresh one. Losing a nice-to-have id must never lose a real query.
    """
    if not draft_id:
        return None
    cur.execute(
        "DELETE FROM requests "
        " WHERE id = %s AND status = 'draft' AND requester_slack_id = %s",
        (draft_id, user_id))
    if cur.rowcount == 1:
        return draft_id
    log.info("submit: draft %s not claimable for %s; using a fresh id",
             draft_id, user_id)
    return None


def create_request(
    prep: Prepared, *, supersedes_id: int | None = None,
    draft_id: int | None = None,
) -> Outcome | Rejection:
    """Resolve auto-approval, insert the request (pending or approved/
    scheduled), write audit rows, and — for an edited resubmission —
    withdraw the superseded original in the SAME transaction.

    Returns a Rejection (not raised) if the per-user rate limit or the
    duplicate guard is tripped at INSERT time by a concurrent submission
    that raced validate_submission()."""
    # Auto-approve resolution. Two grants are checked:
    #   - effective_grant(NOW()) — does the user qualify at submit time?
    #   - if scheduled_for is in the future, also evaluate at that moment;
    #     a grant that expires before the run time must fall back to the
    #     normal approval flow (with a note to the user).
    aa_now = auto_approve.effective_grant(
        prep.user_id, prep.required_mode,
        target_server_id=prep.target.id, database_name=prep.database,
    )
    aa_grant = aa_now
    aa_warn = None
    if aa_now is not None and prep.sched_for is not None:
        aa_at_sched = auto_approve.effective_grant(
            prep.user_id, prep.required_mode,
            target_server_id=prep.target.id, database_name=prep.database,
            at_time=prep.sched_for,
        )
        if aa_at_sched is None:
            aa_grant = None  # fall back to normal approval
            aa_warn = (
                ":warning: Your auto-approve grant expires before the "
                "scheduled run time, so this request needs admin approval."
            )

    # Super-admins have full, self-authorized access: their OWN submissions
    # auto-approve at every tier (RO/RW/DDL), in every channel, always with a
    # full audit trail. Server-enforced from the submitter's identity — never
    # a client-supplied flag. (Scope confirmed with the operator.)
    super_auto = admins.is_super_admin(prep.user_id)

    # Fingerprint approval cache (RO only): a repeat RO query whose
    # parameter-agnostic fingerprint matches a prior completed request
    # (same user/target/db, within TTL) auto-approves too. We still compute
    # the fingerprint so this request seeds the cache, but a super-admin
    # already auto-approves as super — matching the cache on top of that
    # would only mislabel the decision as "fingerprint" and send a redundant
    # auto-approve notification, so skip the lookup for them.
    query_fingerprint: str | None = None
    fp_hit: dict | None = None
    if prep.required_mode == "ro":
        query_fingerprint = ast_safety.fingerprint(prep.query, engine=prep.target.engine)
        if not super_auto and aa_grant is None and query_fingerprint:
            fp_hit = auto_approve.fingerprint_cache_hit(
                prep.user_id, prep.target.id, prep.database, query_fingerprint,
            )

    auto_approved = aa_grant is not None or fp_hit is not None or super_auto
    superseded_row: dict | None = None

    with db.transaction() as cur:
        # Serialize this requester's concurrent submissions so the rate-limit
        # and duplicate guards cannot be raced between validate_submission()'s
        # read and this INSERT. The advisory lock is held to
        # the end of the transaction; a losing racer sees the winner's row.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (prep.user_id,))
        conflict = _recheck_open_limits(cur, prep, supersedes_id)
        if conflict is not None:
            return conflict

        # Take over the id the requester has been looking at since they opened
        # the tab, if it is still theirs to take. Inside this transaction, so a
        # rejection below leaves the draft alone.
        reserved_id = _claim_draft(cur, draft_id, prep.user_id)
        # Only mention `id` in the INSERT when there is one to force; otherwise
        # the sequence default applies exactly as before.
        id_col = "id, " if reserved_id else ""
        id_ph = "%s, " if reserved_id else ""
        id_val: tuple = (reserved_id,) if reserved_id else ()

        if auto_approved:
            # Skip the pending step and write the final approved/scheduled
            # state directly. decided_by uses the AUTO sentinel so the
            # audit + admin queries can spot it.
            new_status = "scheduled" if prep.sched_for is not None else "approved"
            # decided_by: grant / fingerprint auto-approvals use the AUTO
            # sentinel; a super-admin auto-approve records the super's OWN id
            # (they self-authorized with full access) for a precise trail.
            if aa_grant is not None:
                decided_by_id = auto_approve.AUTO_DECIDED_BY
                decided_name = auto_approve.decided_by_name_for(aa_grant)
            elif fp_hit is not None:
                decided_by_id = auto_approve.AUTO_DECIDED_BY
                decided_name = auto_approve.decided_by_name_for_fingerprint(fp_hit["id"])
            else:
                decided_by_id = prep.user_id
                decided_name = f"{prep.user_name or prep.user_id} (super-admin)"
            cur.execute(
                f"INSERT INTO requests "
                f"({id_col}requester_slack_id, requester_name, target_server_id, database_name, "
                f" query, wants_result, result_format, justification, scheduled_for, "
                f" explain_plan, risk_summary, query_fingerprint, status, "
                f" decided_by_slack_id, decided_by_name, decided_at, decision_reason, "
                f" origin, engine, required_tier) "
                f"VALUES ({id_ph}%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, "
                f"        %s, %s, %s, NOW(), %s, %s, %s, %s) "
                "RETURNING id, requester_slack_id, requester_name, target_server_id, "
                "          database_name, query, wants_result, result_format, "
                "          justification, status, scheduled_for, decided_by_slack_id, "
                "          decided_by_name, risk_summary, origin",
                id_val + (
                    prep.user_id,
                    prep.user_name,
                    prep.target.id,
                    prep.database,
                    prep.query,
                    prep.wants_result,
                    prep.result_format,
                    prep.justification,
                    prep.sched_for,
                    json.dumps(prep.explain_plan) if prep.explain_plan is not None else None,
                    prep.risk_summary,
                    query_fingerprint,
                    new_status,
                    decided_by_id,
                    decided_name,
                    decided_name,
                    prep.origin,
                    prep.target.engine,
                    prep.required_mode,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT requests RETURNING produced no row")
            submit_details = {
                "target_alias": prep.target.alias,
                "database": prep.database,
                "wants_result": prep.wants_result,
                "auto_approved": True,
            }
            if prep.origin != "slack":
                submit_details["origin"] = prep.origin
                if prep.client_ip:
                    submit_details["client_ip"] = prep.client_ip
                if prep.user_agent:
                    submit_details["user_agent"] = prep.user_agent
            approve_actor_name = None
            if aa_grant is not None:
                submit_details["grant_id"] = aa_grant["id"]
                submit_details["max_tier"] = aa_grant["max_tier"]
                approve_details = {
                    "grant_id": aa_grant["id"],
                    "max_tier": aa_grant["max_tier"],
                    "scheduled_for": str(prep.sched_for) if prep.sched_for else None,
                }
                approve_action = "auto_approved"
            elif fp_hit is not None:
                submit_details["fingerprint_match_request_id"] = fp_hit["id"]
                approve_details = {
                    "fingerprint_match_request_id": fp_hit["id"],
                    "scheduled_for": str(prep.sched_for) if prep.sched_for else None,
                }
                approve_action = "auto_approved_fingerprint"
            else:
                # Super-admin full access — self-authorized, every tier.
                submit_details["super_admin"] = True
                approve_details = {
                    "reason": "super-admin full access",
                    "tier": prep.required_mode,
                    "scheduled_for": str(prep.sched_for) if prep.sched_for else None,
                }
                approve_action = "auto_approved_super"
                approve_actor_name = decided_name
            audit.log_in(cur, row["id"], prep.user_id, prep.user_name,
                         "submitted", submit_details)
            audit.log_in(cur, row["id"], decided_by_id, approve_actor_name,
                         approve_action, approve_details)
        else:
            cur.execute(
                f"INSERT INTO requests "
                f"({id_col}requester_slack_id, requester_name, target_server_id, database_name, "
                f" query, wants_result, result_format, justification, scheduled_for, "
                f" explain_plan, risk_summary, query_fingerprint, origin, "
                f" engine, required_tier) "
                f"VALUES ({id_ph}%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, "
                f"        %s, %s) "
                "RETURNING id, requester_slack_id, requester_name, target_server_id, "
                "          database_name, query, wants_result, result_format, "
                "          justification, status, scheduled_for, risk_summary, origin",
                id_val + (
                    prep.user_id,
                    prep.user_name,
                    prep.target.id,
                    prep.database,
                    prep.query,
                    prep.wants_result,
                    prep.result_format,
                    prep.justification,
                    prep.sched_for,
                    json.dumps(prep.explain_plan) if prep.explain_plan is not None else None,
                    prep.risk_summary,
                    query_fingerprint,
                    prep.origin,
                    prep.target.engine,
                    prep.required_mode,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT requests RETURNING produced no row")
            submit_details = {
                "target_alias": prep.target.alias,
                "database": prep.database,
                "wants_result": prep.wants_result,
            }
            if prep.origin != "slack":
                submit_details["origin"] = prep.origin
                if prep.client_ip:
                    submit_details["client_ip"] = prep.client_ip
                if prep.user_agent:
                    submit_details["user_agent"] = prep.user_agent
            audit.log_in(cur, row["id"], prep.user_id, prep.user_name,
                         "submitted", submit_details)

        if supersedes_id is not None and supersedes_id != row["id"]:
            # Withdraw the edited-away original — owner + still-pending
            # enforced in the WHERE, so a race with an admin decision
            # simply leaves it in that decided state.
            cur.execute(
                f"UPDATE requests SET status = 'cancelled', "
                f" decision_reason = %s, decided_at = NOW() "
                f"WHERE id = %s AND status = 'pending' "
                f"  AND requester_slack_id = %s "
                f"RETURNING {REQUEST_RETURNING}",
                (f"superseded by request #{row['id']} (edited by requester)",
                 supersedes_id, prep.user_id),
            )
            superseded_row = cur.fetchone()
            if superseded_row is not None:
                audit.log_in(cur, supersedes_id, prep.user_id, prep.user_name,
                             "cancelled", {"by": "requester",
                                           "phase": "pending",
                                           "superseded_by": row["id"]})

    return Outcome(
        row=row,
        auto_approved=auto_approved,
        aa_grant=aa_grant,
        fp_hit=fp_hit,
        aa_warn=aa_warn,
        supersedes_id=supersedes_id,
        superseded_row=superseded_row,
    )


# ---- Step 3: notifications + dispatch --------------------------------------

def dispatch_and_notify(
    client,
    prep: Prepared,
    outcome: Outcome,
    *,
    dm_requester: bool = True,
) -> str:
    """Admin-facing fan-out + execution dispatch. `dm_requester=False`
    suppresses the requester-facing Slack DMs (the web UI shows status
    itself) — admin notifications and dispatch always happen.

    Returns one of: dispatched | scheduled | pending | no_admins."""
    # Imported lazily: notifications lives in the slack_app package and
    # pulls Bolt-adjacent modules the web process doesn't need at import.
    from . import executor
    from .slack_app import notifications

    row = outcome.row

    if outcome.auto_approved:
        if outcome.aa_grant is not None:
            notifications.dm_admins_auto_approved(
                client, row, prep.target, outcome.aa_grant)
            why = (f"(grant #{outcome.aa_grant['id']}, "
                   f"max_tier={outcome.aa_grant['max_tier']})")
        elif outcome.fp_hit is not None:
            notifications.dm_admins_auto_approved_fingerprint(
                client, row, prep.target, outcome.fp_hit)
            why = f"(matches your completed request #{outcome.fp_hit['id']})"
        else:
            # Super-admin self-authorized auto-approve: no grant and no
            # fingerprint match (create_request set super_auto). Full audit is
            # already written ('auto_approved_super'); there's no grant or
            # fingerprint to describe, and the super IS an admin, so skip the
            # admin auto-approve DM rather than crash on a None fingerprint.
            why = "(super-admin full access)"
        if dm_requester:
            sched_line = ""
            if row.get("scheduled_for"):
                sched_line = (
                    f"\n*Scheduled for:* `{row['scheduled_for']:%Y-%m-%d %H:%M UTC}` "
                    "(auto-approved)"
                )
            notifications.dm_requester(
                client, prep.user_id,
                f":zap: *SQL query request `#{row['id']}` auto-approved* {why}."
                + sched_line + "\n"
                + notifications.request_context_with_query_md(row),
            )
        if prep.sched_for is None:
            executor.submit(row, client)
            return "dispatched"
        # Scheduled auto-approved — the scheduler picks it up on time.
        if dm_requester:
            notifications.dm_user_scheduled(client, row)
        return "scheduled"

    if not admins.list_active():
        if dm_requester:
            notifications.dm_requester(
                client, prep.user_id,
                f":warning: *SQL query request `#{row['id']}` saved* but no "
                "admins are configured to approve it.\n"
                + notifications.request_context_md(row)
                + "\n\nContact the DBA team.",
            )
        else:
            log.warning("request %s saved but no admins are configured", row["id"])
        return "no_admins"

    notifications.notify_admins(client, row)
    if dm_requester:
        blocks = notifications.requester_card_blocks(
            row,
            status_emoji=":hourglass_flowing_sand:",
            status_text="Submitted — waiting for admin approval",
            body_extra=outcome.aa_warn,
            with_cancel=True,
        )
        notifications.dm_requester_card_tracked(
            client, row,
            text=f"SQL request #{row['id']} submitted — waiting for admin approval.",
            blocks=blocks,
        )
    return "pending"
