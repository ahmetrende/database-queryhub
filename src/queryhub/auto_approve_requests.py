"""Persistence for auto-approve window requests (the RO-burst nudge).

Mirrors `access_requests.py`: a small CRUD surface over the
`auto_approve_requests` table. The Slack UI lives in
`slack_app/ro_window.py`; the Bolt registrations in `slack_app/handlers.py`.

This module imports `slack_app` in one place only: `_dm_admins`, which runs
only for a caller that has a Slack client. The web routes use this module
also when Slack is off or when the `slack_app` package is not there.
"""
from __future__ import annotations

import logging

from . import audit, db, idp_outbox, window_options
from . import config as cfg

log = logging.getLogger(__name__)


def find_pending_for(principal_id: str, target_server_id: int) -> dict | None:
    """The user's open (pending) window request for this target, if any —
    enforces the one-pending-per-(user, target) rule before insert."""
    return db.fetch_one(
        "SELECT id, created_at FROM auto_approve_requests "
        " WHERE requester_slack_id = %s AND target_server_id = %s "
        "   AND status = 'pending'",
        (principal_id, target_server_id),
    )


def create(
    *,
    principal_id: str,
    name: str | None,
    target_server_id: int,
    database_name: str | None,
    max_tier: str,
    window_minutes: int,
    reason: str,
) -> dict | None:
    """Insert a pending request. Returns the row, or None if a pending
    request for the same (user, target) already exists (race with the
    partial unique index)."""
    return db.fetch_one(
        "INSERT INTO auto_approve_requests "
        "  (requester_slack_id, requester_name, target_server_id, "
        "   database_name, max_tier, window_minutes, reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING "
        "RETURNING id, requester_slack_id, requester_name, target_server_id, "
        "          database_name, max_tier, window_minutes, reason, "
        "          status, created_at",
        (principal_id, name, target_server_id, database_name,
         max_tier, window_minutes, reason),
    )


def get(request_id: int) -> dict | None:
    return db.fetch_one(
        "SELECT id, requester_slack_id, requester_name, target_server_id, "
        "       database_name, max_tier, window_minutes, reason, status, "
        "       decided_by_slack_id, decided_by_name, decided_at, granted_id, "
        "       created_at "
        "  FROM auto_approve_requests WHERE id = %s",
        (request_id,),
    )


_DECIDE_SQL = (
    "UPDATE auto_approve_requests "
    "   SET status = %s, decided_by_slack_id = %s, decided_by_name = %s, "
    "       granted_id = %s, decided_at = NOW() "
    " WHERE id = %s AND status = 'pending' "
    "RETURNING id, requester_slack_id, target_server_id, database_name, "
    "          max_tier, window_minutes, status")


def decide_in(
    cur,
    request_id: int,
    *,
    status: str,
    decided_by_slack_id: str,
    decided_by_name: str | None,
    granted_id: int | None = None,
) -> dict | None:
    """`decide` on the caller's cursor. The decision commits in the caller's
    transaction, together with its audit row."""
    cur.execute(_DECIDE_SQL, (status, decided_by_slack_id, decided_by_name,
                              granted_id, request_id))
    return cur.fetchone()


def decide(
    request_id: int,
    *,
    status: str,
    decided_by_slack_id: str,
    decided_by_name: str | None,
    granted_id: int | None = None,
) -> dict | None:
    """Move a pending request to approved/rejected. Only acts on a still
    -pending row (so two admins can't both decide it). Returns the updated
    row, or None if it was already decided."""
    with db.transaction() as cur:
        return decide_in(cur, request_id, status=status,
                         decided_by_slack_id=decided_by_slack_id,
                         decided_by_name=decided_by_name, granted_id=granted_id)


class WindowRequestRefused(Exception):
    """A window request that cannot be made, with the field it is about and
    the HTTP status a web caller should answer with."""

    def __init__(self, field: str, message: str, status: int = 400):
        super().__init__(message)
        self.field, self.message, self.status = field, message, status


# The web offers whole days as well as Slack's short windows: a day-long window
# is for a piece of work, an hour-long one for a burst of reads.
DAY_WINDOWS = (1, 7, 14, 30)
_TIER_RANK = {"ro": 1, "rw": 2, "ddl": 3}

# The switch for windows that cover writes (migration 150). Off by default.
RW_REQUESTS_KEY = "auto_approve_rw_requests"
_ON = {"on", "true", "yes", "1"}

RW_OFF_MESSAGE = ("Auto-approve for writes (RW) is not available. "
                  "You can ask for read-only (RO) auto-approve.")
RW_OFF_DECISION_MESSAGE = ("Auto-approve for writes (RW) is not available, so "
                           "this window cannot be approved. You can reject it.")
DDL_MESSAGE = "Schema changes are always reviewed and cannot be auto-approved."
PRODUCTION_WRITE_MESSAGE = ("Only a super-admin can decide a window that covers "
                            "writes on a production connection.")
RW_NOT_HELD_MESSAGE = ("The requester does not hold RW here now, so a window "
                       "for writes cannot be approved. You can reject it.")


def rw_requests_enabled() -> bool:
    """True when people may ask for an auto-approve window that covers writes.

    A read that fails counts as off. A switch that opens a way around review
    must stay closed when nobody can say that it is open."""
    try:
        # The literal key, not RW_REQUESTS_KEY: tests/test_config_keys_seeded
        # reads the literal and checks that a migration seeds it.
        value = cfg.get_setting("auto_approve_rw_requests", "off")
    except Exception:
        log.exception("could not read %s. RW window requests stay off",
                      RW_REQUESTS_KEY)
        return False
    return (value or "").strip().lower() in _ON


def offered_tiers() -> list[str]:
    """The tiers a person can ask a window for, in the words the web uses."""
    return ["RO", "RW"] if rw_requests_enabled() else ["RO"]


def _held_rank(principal_id: str, target_id: int, db_scope: str | None) -> int:
    """The rank of the tier the person holds, per database when one is named.
    Asked of the resolver that the submit path uses. 0 means no access."""
    from . import teams
    held = (teams.effective_mode_for_database(principal_id, target_id, db_scope)
            if db_scope else
            ((teams.effective_grant_for_user(principal_id, target_id) or {}).get("mode")))
    return _TIER_RANK.get((held or "").lower(), 0)


def on_production(alias: str | None) -> bool:
    """True when the connection counts as production (`targets.environment`).
    No alias, for example a deleted connection, counts as production."""
    from . import targets
    return targets.environment(alias) == "production"


def needs_super_admin(req: dict, alias: str | None) -> bool:
    """A window that covers writes on production is a super-admin's decision.

    Only a read-only window stays with every admin in scope. A `ddl` row
    cannot be asked for, but if one exists, it counts as a write here."""
    return (req.get("max_tier") or "ro").lower() != "ro" and on_production(alias)


def can_decide(actor_id: str, req: dict, alias: str | None) -> bool:
    """True when this admin may decide this window request.

    The scope check is the one a query approval uses, because the waiver gives
    exactly that. A window that covers writes on production also needs a
    super-admin. The admin screen lists only the requests that pass this."""
    from . import admins
    if needs_super_admin(req, alias) and not admins.is_super_admin(actor_id):
        return False
    return admins.can_approve(actor_id, {
        "required_tier": req.get("max_tier"),
        "target_server_id": req.get("target_server_id"),
        "requester_slack_id": req.get("requester_slack_id")})


def valid_window(minutes: int) -> bool:
    return (window_options.is_valid_window(minutes)
            or minutes in {d * 1440 for d in DAY_WINDOWS})


def submit_window(*, principal_id: str, name: str | None, target_id: int,
                  window_minutes: int, reason: str,
                  database_name: str | None = None, tier: str = "ro"):
    """Check and persist one window request.

    The rules the Slack modal always enforced, in one place so the web form
    enforces the same ones: the bot is not halted, the window is one of the
    offered lengths, the reason says something, the requester can reach the
    target (and the database, when one is named -- the approval carries it into
    the waiver), there is not already a request pending for it, and the target
    offers windows at all (an Athena archive does not, unless its engine_config
    turns auto-approve on).

    A window that covers writes (RW) needs the `auto_approve_rw_requests`
    switch on, and RW access that the requester holds now. While the switch is
    off, only a read-only window can be asked for. DDL is never offered.

    Returns (row, target). Notifying the admins is the caller's, because it
    needs a Slack client and the two surfaces hold theirs differently.
    """
    from . import auto_approve, core_submit, engines, targets, teams
    if core_submit.kill_switch_on():
        raise WindowRequestRefused("reason", core_submit.kill_switch_message(), 503)
    tier = (tier or "ro").strip().lower()
    if tier == "ddl":
        raise WindowRequestRefused("tier", DDL_MESSAGE)
    if tier not in ("ro", "rw"):
        raise WindowRequestRefused("tier", "Pick RO or RW.")
    if tier == "rw" and not rw_requests_enabled():
        raise WindowRequestRefused("tier", RW_OFF_MESSAGE, 403)
    if not valid_window(window_minutes):
        raise WindowRequestRefused("window", "Pick a valid window length.")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise WindowRequestRefused(
            "reason", "Please give a meaningful reason (at least 5 characters).")
    # A forged submission must not be able to ask for a window on something
    # the requester cannot reach; the pickers only list what they can.
    if not teams.can_use_target(principal_id, target_id):
        raise WindowRequestRefused("target", "You don't have access to that target.", 403)
    db_scope = auto_approve.normalise_scope(database_name)
    if db_scope is not None:
        if not teams.can_use_database(principal_id, target_id, db_scope):
            raise WindowRequestRefused(
                "database", "You don't have access to that database.", 403)
        try:
            auto_approve.validate_scope(target_id, db_scope)
        except auto_approve.ScopeError as e:
            raise WindowRequestRefused("database", str(e))
    if find_pending_for(principal_id, target_id) is not None:
        raise WindowRequestRefused(
            "target", "You already have a pending window request for this target.", 409)
    t = targets.get(target_id)
    if t is None:
        raise WindowRequestRefused("target", "That target no longer exists.", 404)
    # A window is a waiver that names its target, and on an Athena archive such
    # a waiver applies (auto_approve.waiver_applies). None is offered there all
    # the same. The rule on an archive is that a member's queries go to the
    # owning team's lead and an admin, and a window, which the read-burst nudge
    # invites anyone to ask for, would take them out of that review. The lead
    # and the admins need none: their role already lets their reads through
    # there (auto_approve.decision). An admin who decides otherwise for one
    # person can still write a waiver that names the archive.
    if not engines.auto_approve_allowed(t):
        raise WindowRequestRefused(
            "target", "Auto-approve windows are not offered on this connection; "
            "its queries are reviewed by an approver.", 409)
    if tier == "rw":
        # A waiver covers what the access allows and no more, so asking to skip
        # review on writes needs write access to start with. Asked of the
        # resolver, per database when one is named.
        if _held_rank(principal_id, target_id, db_scope) < _TIER_RANK["rw"]:
            raise WindowRequestRefused(
                "tier", "You hold read-only here, so only reads can be auto-approved.", 403)
    row = create(principal_id=principal_id, name=name, target_server_id=target_id,
                 database_name=db_scope, max_tier=tier,
                 window_minutes=window_minutes, reason=reason)
    if row is None:              # lost the race with the partial unique index
        raise WindowRequestRefused(
            "target", "A pending window request for this target already exists.", 409)
    return row, t


def notify_admins(client, row: dict, alias: str) -> int:
    """DM every active admin the approve / reject card. Returns how many were
    reached. Zero means nobody can decide it.

    It also writes the IdP panel's `queryhub.window_request_pending` row while
    `idp_outbox_enabled` is on. That row names the admins that the DMs go to,
    and it does not need Slack. With no client, the function still writes the
    row and returns 0. It then does not import `slack_app`."""
    from . import admins
    outbox_on = idp_outbox.enabled()
    if client is None and not outbox_on:
        return 0
    # One list for the DMs and for the outbox row, so that both name the same
    # admins. The row goes first: it must not depend on Slack delivery.
    admins_told = admins.list_active()
    if outbox_on:
        idp_outbox.window_request_pending(row, alias, admins_told)
    if client is None:
        return 0
    return _dm_admins(client, row, alias, admins_told)


def _dm_admins(client, row: dict, alias: str, admins_told: list[dict]) -> int:
    """The Slack half of `notify_admins`: send the approve / reject card to
    each admin in `admins_told`. Returns the number of admins that it reached.

    Only a caller with a Slack client gets here. So this is the one place in
    this module that imports `slack_app`."""
    from .slack_app import notifications, ro_window
    blocks = ro_window.admin_dm_blocks(row, alias)
    fallback = (f"{(row.get('max_tier') or 'ro').upper()} auto-approve window request "
                f"from <@{row['requester_slack_id']}> for {alias} (#{row['id']})")
    reached = 0
    for a in admins_told:
        try:
            opened = client.conversations_open(users=a["slack_user_id"])
            notifications._post(client, channel=opened["channel"]["id"],
                                text=fallback, blocks=blocks)
            reached += 1
        except Exception:
            log.exception("ro_window: admin DM failed for %s", a["slack_user_id"])
    return reached


def list_for(principal_id: str, limit: int = 50) -> list[dict]:
    """A requester's own window requests, newest first."""
    return db.fetch_all(
        "SELECT r.id, r.target_server_id, t.alias, r.database_name, r.max_tier, "
        "       r.window_minutes, r.reason, r.status, r.decided_by_name, "
        "       r.decided_at, r.created_at "
        "  FROM auto_approve_requests r "
        "  LEFT JOIN target_servers t ON t.id = r.target_server_id "
        " WHERE r.requester_slack_id = %s "
        " ORDER BY r.created_at DESC LIMIT %s",
        (principal_id, limit))


class WindowDecisionRefused(Exception):
    """A decision that cannot be made. `code` names a rule of the window
    itself (`super_admin`, `ddl`, `rw_off`, `rw_not_held`, `no_window`), so
    that a surface can say that rule. It is None for the older refusals:
    not an admin, no such request, already decided, outside the scope."""

    def __init__(self, message: str, status: int = 409, code: str | None = None):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def decide_window(request_id: int, *, approve: bool, actor_id: str,
                  actor_name: str | None) -> dict:
    """Approve or decline a pending window request -- the Slack card and the web
    admin screen, one rulebook.

    Approving writes the waiver with its window starting NOW, at the decision,
    not at the ask. A scoped admin is held to the same tier, target and team
    scope as a query approval, because the waiver hands out exactly that. Two
    admins deciding at once: the second finds the request already decided.
    Returns {status, request, grant_id?, alias}.

    A window that covers writes has more rules. On a production connection
    only a super-admin decides it, to approve or to reject. Approval also needs
    the `auto_approve_rw_requests` switch on, and RW access that the requester
    holds at the time of the decision. A DDL window is never approved. Every
    waiver ends: the window length is the time to its expiry.

    Because both surfaces decide through here, this is the one place that
    writes the IdP panel's `queryhub.window_request_decided` row, after the
    commit.
    """
    import json
    from . import admins, auto_approve, targets
    if not admins.is_admin(actor_id):
        raise WindowDecisionRefused("Admin access required.", 403)
    req = get(request_id)
    if req is None:
        raise WindowDecisionRefused("No such request.", 404)
    if req["status"] != "pending":
        raise WindowDecisionRefused(f"Already {req['status']}.")
    t = targets.get(req["target_server_id"])
    alias = t.alias if t else f"target #{req['target_server_id']}"
    if needs_super_admin(req, t.alias if t else None) \
            and not admins.is_super_admin(actor_id):
        raise WindowDecisionRefused(PRODUCTION_WRITE_MESSAGE, 403, code="super_admin")
    if not approve:
        # One transaction: the decision and its audit row commit together, as
        # they do on the approve path below. They were two commits, so a failure
        # between them left a rejected request with no audit row.
        with db.transaction() as cur:
            decided = decide_in(cur, request_id, status="rejected",
                                decided_by_slack_id=actor_id,
                                decided_by_name=actor_name)
            if decided is None:
                raise WindowDecisionRefused("Already decided.")
            audit.log_in(cur, None, actor_id, actor_name,
                         "auto_approve_window_rejected",
                         {"request_id": request_id,
                          "user": decided["requester_slack_id"]})
        _tell_the_panel(req, approved=False, alias=alias,
                        actor_id=actor_id, actor_name=actor_name)
        return {"status": "rejected", "request": req, "alias": alias}
    if not admins.can_approve(actor_id, {"required_tier": req["max_tier"],
                                         "target_server_id": req["target_server_id"],
                                         "requester_slack_id": req["requester_slack_id"]}):
        raise WindowDecisionRefused(
            "That request is outside your admin scope. Ask an admin with broader scope.", 403)
    tier = (req["max_tier"] or "ro").lower()
    if tier == "ddl":
        # submit_window never files one. A row from somewhere else is refused
        # here, at the point where the waiver would be written.
        raise WindowDecisionRefused(DDL_MESSAGE, 409, code="ddl")
    if tier == "rw":
        if not rw_requests_enabled():
            raise WindowDecisionRefused(RW_OFF_DECISION_MESSAGE, 409, code="rw_off")
        # A waiver skips the review and grants nothing. Access can change
        # between the ask and the decision, so it is asked again now.
        if _held_rank(req["requester_slack_id"], req["target_server_id"],
                      auto_approve.normalise_scope(req["database_name"])) \
                < _TIER_RANK["rw"]:
            raise WindowDecisionRefused(RW_NOT_HELD_MESSAGE, 409, code="rw_not_held")
    minutes = req.get("window_minutes")
    if not isinstance(minutes, int) or minutes <= 0:
        # The table refuses such a row (migration 052). A waiver with no end
        # is never written from a window request.
        raise WindowDecisionRefused("This request has no window length, so it "
                                    "cannot be approved.", 409, code="no_window")
    with db.connection() as conn:
        with conn.cursor() as cur:
            # The requester is told by the caller, in words about the window;
            # the auth-event trigger would say it a second time.
            cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
            cur.execute(
                "INSERT INTO auto_approve_grants "
                "  (slack_user_id, max_tier, target_server_id, database_name, "
                "   expires_at, reason, granted_by) "
                "VALUES (%s, %s, %s, %s, NOW() + make_interval(mins => %s), %s, %s) "
                "RETURNING id, expires_at",
                (req["requester_slack_id"], req["max_tier"], req["target_server_id"],
                 auto_approve.normalise_scope(req["database_name"]), minutes,
                 f"window request #{request_id}: {req['reason']}", actor_id))
            granted = cur.fetchone()
            grant_id = granted["id"]
            expires_at = granted.get("expires_at")
            cur.execute(
                "UPDATE auto_approve_requests SET status='approved', "
                "  decided_by_slack_id=%s, decided_by_name=%s, granted_id=%s, "
                "  decided_at=NOW() WHERE id=%s AND status='pending'",
                (actor_id, actor_name, grant_id, request_id))
            if cur.rowcount == 0:
                conn.rollback()
                raise WindowDecisionRefused("Already decided.")
            cur.execute(
                "INSERT INTO audit_log (actor_slack_id, actor_name, action, details) "
                "VALUES (%s, %s, 'auto_approve_window_approved', %s::jsonb)",
                (actor_id, actor_name, json.dumps({
                    "request_id": request_id, "grant_id": grant_id,
                    "user": req["requester_slack_id"],
                    "target_server_id": req["target_server_id"],
                    "database_name": req["database_name"],
                    "max_tier": req["max_tier"], "window_minutes": minutes,
                    # When the waiver ends, from the row the INSERT wrote.
                    **({"expires_at": expires_at.isoformat()}
                       if hasattr(expires_at, "isoformat") else {})})))
        conn.commit()
    _tell_the_panel(req, approved=True, alias=alias,
                    actor_id=actor_id, actor_name=actor_name)
    return {"status": "approved", "request": req, "grant_id": grant_id, "alias": alias}


def _tell_the_panel(req: dict, *, approved: bool, alias: str, actor_id: str,
                    actor_name: str | None) -> None:
    """The IdP panel's copy of a committed window decision. It is best effort:
    `idp_outbox` logs a failure and never raises it."""
    if idp_outbox.enabled():
        idp_outbox.window_request_decided(req, approved=approved, alias=alias,
                                          decider_name=actor_name or actor_id)


def list_pending(limit: int = 200) -> list[dict]:
    """Every request still waiting for a decision, oldest first -- the order an
    approver works through them."""
    return db.fetch_all(
        "SELECT r.id, r.requester_slack_id, r.requester_name, r.target_server_id, "
        "       t.alias, r.database_name, r.max_tier, r.window_minutes, r.reason, "
        "       r.status, r.created_at "
        "  FROM auto_approve_requests r "
        "  LEFT JOIN target_servers t ON t.id = r.target_server_id "
        " WHERE r.status = 'pending' ORDER BY r.created_at LIMIT %s", (limit,))
