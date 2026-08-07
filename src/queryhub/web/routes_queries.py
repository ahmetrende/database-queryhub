"""The heart of the web API: submit → (Slack approval) → status → result.

POST /queries runs the SAME core_submit pipeline as the Slack modal —
validation, grants, pre-flight, auto-approve, INSERT + audit, admin
fan-out, dispatch. The web adds: session auth, the live users.info
check before RW/DDL (AUTH.md §5), and contract-shaped JSON. Results
are served from the executor's stored file, which is masked already —
raw PII never reaches the browser.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from .. import admins, audit as audit_mod
from .. import cancellation, core_submit, db, pii, pre_flight, profile_sync, query_safety, requesters
from .. import config as cfg
from . import deps, mapping, sessions
from .routes_data import _alias_of, _target_by_alias

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(deps.block_pw_gate)])

_slack_client = None


def _bot_client():
    """The web process's own Slack client — used for admin fan-out and
    executor delivery, exactly like the bot process."""
    global _slack_client
    if not cfg.ENV.slack_enabled:
        return None  # vanilla profile: no Slack client; delivery/DMs no-op
    if _slack_client is None:
        from slack_sdk import WebClient
        _slack_client = WebClient(token=cfg.ENV.slack_bot_token)
    return _slack_client


# ---- POST /queries -----------------------------------------------------------

class ScheduleIn(BaseModel):
    runAt: str


class QueryIn(BaseModel):
    connectionId: str
    databaseId: str | None = None
    sql: str = Field(min_length=1, max_length=200_000)
    name: str | None = None
    justification: str | None = None
    schedule: ScheduleIn | None = None
    # The id this tab reserved when it opened (POST /queries/draft). Optional:
    # an older client, or a draft that has since been reaped, simply gets a
    # fresh id — see core_submit._claim_draft.
    draftId: int | None = None


class BatchItemIn(BaseModel):
    connectionId: str
    databaseId: str | None = None
    sql: str = Field(min_length=1, max_length=200_000)
    name: str | None = None


class BatchIn(BaseModel):
    items: list[BatchItemIn] = Field(min_length=1)
    justification: str | None = None
    schedule: ScheduleIn | None = None


def _schedule_parts(claims: dict, runat_iso: str) -> tuple[str, str]:
    """Contract sends UTC ISO; core expects wall-clock date+time in the
    user's profile timezone (it converts back to UTC internally)."""
    dt = datetime.fromisoformat(runat_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz_name = profile_sync.lookup_tz(claims["sub"]) or "UTC"
    try:
        local = dt.astimezone(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        local = dt.astimezone(timezone.utc)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


_REJECTION_HTTP = {
    # field/reason → (status, contract error code)
    "tier_exceeds": (403, "forbidden"),
    "duplicate": (409, "conflict"),
    "server": (403, "forbidden"),
    "database": (403, "forbidden"),
    "rate_limit": (409, "conflict"),
    "kill_switch": (503, "server_error"),
}


def _reject(rej: core_submit.Rejection):
    status, code = _REJECTION_HTTP.get(
        rej.reason or rej.field, (422, "validation"))
    raise deps._error(status, code, rej.message.replace("*", ""))


def _client_ctx(request: Request) -> tuple[str | None, str | None]:
    """Client IP + user-agent for the audit trail (web-origin submits only).

    IP comes from deps.client_ip, which trusts X-Forwarded-For only behind a
    configured reverse proxy (web_trusted_proxy) — otherwise the header is
    client-spoofable and would poison the audit trail. UA is
    length-capped so a hostile client can't bloat the audit JSON."""
    ip = deps.client_ip(request)
    ua = request.headers.get("user-agent")
    if ua and len(ua) > 400:
        ua = ua[:400]
    return ip, ua


@router.post("/queries/draft", status_code=201)
def reserve_query_id(claims: dict = Depends(deps.current_user)):
    """Reserve the id a new query tab will submit under.

    So the number on screen is the request id from the first keystroke rather
    than appearing after submit. The row is a draft: no target, no SQL, and
    invisible to every reporting view (migration 086).

    Same gates as submitting — this writes a row, so it is not open to a
    half-authorized session.
    """
    deps.require_whitelisted(claims)
    deps.block_if_password_change_required(claims)
    return {"id": core_submit.reserve_request_id(claims["sub"])}


@router.post("/queries", status_code=201)
def submit_query(body: QueryIn, request: Request,
                 claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    deps.block_if_password_change_required(claims)
    uid = claims["sub"]

    t = _target_by_alias(body.connectionId)
    if t is None:
        raise deps._error(404, "not_found",
                          f"Unknown connection '{body.connectionId}'.")
    # Disabled targets are admin-only, mirroring the Slack picker. An
    # existing-but-ungranted target is reported as 404 too (identical to
    # unknown) so a whitelisted user can't enumerate real server aliases
    # by distinguishing 403 (exists, no grant) from 404 (doesn't exist).
    # The grant is still authoritatively enforced by core_submit.
    from .. import admins, teams
    ungranted = teams.effective_grant_for_user(uid, t.id) is None
    if (not t.enabled and not admins.is_admin(uid)) or ungranted:
        raise deps._error(404, "not_found",
                          f"Unknown connection '{body.connectionId}'.")

    sched_date = sched_time = None
    if body.schedule is not None:
        try:
            sched_date, sched_time = _schedule_parts(claims, body.schedule.runAt)
        except ValueError:
            raise deps._error(422, "validation", "Invalid schedule.runAt.")

    client_ip, user_agent = _client_ctx(request)
    prep = core_submit.validate_submission(
        uid,
        claims.get("name") or uid,
        target_server_id=t.id,
        database_name=body.databaseId,
        query=body.sql,
        justification=body.justification,
        wants_result=True,
        result_format="csv",
        schedule_date=sched_date,
        schedule_time=sched_time,
        origin="web",
        client_ip=client_ip,
        user_agent=user_agent,
    )
    if isinstance(prep, core_submit.Rejection):
        _reject(prep)

    # AUTH.md §5 — live employment check at the dangerous moment. Slack-only:
    # a local account has no external employment system, so its liveness is the
    # enabled requester/admin row (re-checked at refresh), not users.info.
    if prep.required_mode in ("rw", "ddl") and claims.get("provider") == "slack":
        if not deps.slack_employment_ok(uid):
            from . import sessions
            sessions.revoke_user(uid, "users.info failed before RW/DDL submit")
            raise deps._error(401, "unauthenticated",
                              "Slack account verification failed.")

    outcome = core_submit.create_request(prep, draft_id=body.draftId)
    if isinstance(outcome, core_submit.Rejection):
        # A concurrent submission raced the rate-limit / duplicate guard and
        # lost at INSERT time.
        _reject(outcome)
    # Requester feedback lives in the web UI — dm_requester=False keeps
    # admin fan-out + dispatch identical to Slack submits.
    core_submit.dispatch_and_notify(_bot_client(), prep, outcome,
                                    dm_requester=False)

    row = db.fetch_one(
        "SELECT id, status, created_at FROM requests WHERE id = %s",
        (outcome.row["id"],))
    resp = {
        "id": str(row["id"]),
        "status": mapping.status_to_web(row["status"]),
        "classification": mapping.classification_of(prep.query),
        "pii": mapping.pii_preview(prep.query),
        "decision": "auto_approve" if outcome.auto_approved else "needs_approval",
        "createdAt": mapping.iso(row["created_at"]),
    }
    if not outcome.auto_approved:
        resp["approval"] = {"channel": "DBA admins (Slack)",
                            "slackMessageTs": None}
        # aa_warn only matters when the request actually fell back to
        # approval; a fingerprint auto-approve can set it while still
        # dispatching, and showing "needs admin approval" then is wrong.
        if outcome.aa_warn:
            resp["note"] = outcome.aa_warn.replace(":warning: ", "")
    return resp


# ---- POST /feedback (bug report / idea from the web) -------------------------

class FeedbackIn(BaseModel):
    type: str = "idea"                                    # "bug" | "idea"
    subject: str = Field(min_length=1, max_length=200)
    details: str = Field(min_length=1, max_length=5000)
    severity: str | None = None                           # low|med|high (bug only)
    view: str | None = None                               # "dev" | "admin" (context)


@router.post("/feedback", status_code=201)
def submit_feedback(body: FeedbackIn, request: Request,
                    claims: dict = Depends(deps.current_user)):
    """A developer's bug report / idea from the web. Recorded to the audit log
    and DM'd to the active admins so it actually reaches the team (the modal
    tells the user it was 'sent to the QueryHub team')."""
    deps.require_whitelisted(claims)
    uid = claims["sub"]
    name = claims.get("name") or uid
    kind = "bug" if body.type == "bug" else "idea"
    client_ip, user_agent = _client_ctx(request)
    details = {
        "type": kind,
        "subject": body.subject.strip()[:200],
        "details": body.details.strip()[:5000],
        "severity": (body.severity or None) if kind == "bug" else None,
        "view": body.view,
        "user_agent": user_agent,
        "client_ip": client_ip,
    }
    with db.transaction() as cur:
        audit_mod.log_in(cur, None, uid, name, "web_feedback", details)
    # Best-effort: DM the active admins so the report actually reaches someone.
    try:
        from ..slack_app import notifications
        sev = (" · severity *" + body.severity + "*") if (kind == "bug" and body.severity) else ""
        head = (f":lady_beetle: *Bug report* from <@{uid}>{sev}" if kind == "bug"
                else f":bulb: *Feedback* from <@{uid}>")
        text = (f"{head}\n*{body.subject.strip()}*\n{body.details.strip()[:1500]}\n"
                f"_via QueryHub Web ({body.view or 'dev'})_")
        for adm in admins.list_active():
            try:
                notifications.dm_requester(_bot_client(), adm["slack_user_id"], text)
            except Exception:
                log.exception("feedback DM to admin %s failed", adm.get("slack_user_id"))
    except Exception:
        log.exception("feedback admin notify failed")
    return {"ok": True}


# ---- Notifications (developer bell) ------------------------------------------
#
# The feed is DERIVED on read — approval decisions on the user's own requests,
# scheduled runs that completed, endpoint-request decisions, and fleet
# kill-switch flips. Nothing is stored per notification; only the READ state
# persists (web_notification_reads) so the unread badge follows the user
# across devices. Ids are deterministic (q<id>-dec, q<id>-sched, er<id>,
# kill<audit_id>) so read-marks survive re-derivation.

def _derive_notifications(uid: str) -> list[dict]:
    items: list[dict] = []
    rows = db.fetch_all(
        "SELECT r.id, r.status, r.decided_by_name, r.decided_at, r.decision_reason, "
        "       r.row_count, r.database_name, r.scheduled_for, r.completed_at, "
        "       s.alias "
        "FROM requests r LEFT JOIN target_servers s ON s.id = r.target_server_id "
        "WHERE r.requester_slack_id = %s AND r.status <> 'draft' "
        "  AND r.created_at > now() - interval '30 days' "
        "ORDER BY r.id DESC LIMIT 120", (uid,))
    for r in rows:
        loc = "/".join(str(b) for b in [r["alias"], r["database_name"]] if b) or "a target"
        if (r["status"] in ("rejected", "changes_requested")) and r["decided_at"]:
            title = "Changes requested" if r["status"] == "changes_requested" else "Query rejected"
            why = (" — “" + str(r["decision_reason"])[:160] + "”") if r["decision_reason"] else ""
            items.append({"id": f"q{r['id']}-dec", "kind": "rejected", "title": title,
                          "body": f"{r['decided_by_name'] or 'An admin'} declined your query on {loc}{why}.",
                          "createdAt": mapping.iso(r["decided_at"])})
        elif r["decided_by_name"] and r["decided_at"]:
            auto = str(r["decided_by_name"]).lower().startswith(("auto", "AUTO".lower()))
            if auto:
                body = f"Your query on {loc} ran under an auto-approve grant"
                title = "Query auto-approved"
            else:
                body = f"{r['decided_by_name']} approved your query on {loc}"
                title = "Query approved"
            if r["status"] == "completed" and r["row_count"] is not None:
                body += f" — {int(r['row_count']):,} rows"
            items.append({"id": f"q{r['id']}-dec", "kind": "approved", "title": title,
                          "body": body + ".", "createdAt": mapping.iso(r["decided_at"])})
        if r["scheduled_for"] and r["completed_at"]:
            items.append({"id": f"q{r['id']}-sched", "kind": "scheduled", "title": "Scheduled query ran",
                          "body": f"Your scheduled query on {loc} finished — {int(r['row_count'] or 0):,} rows.",
                          "createdAt": mapping.iso(r["completed_at"])})
    for r in db.fetch_all(
            "SELECT e.id, e.status, e.decided_at, e.database_name, s.alias "
            "FROM access_requests e LEFT JOIN target_servers s ON s.id = e.target_server_id "
            "WHERE e.requester_slack_id = %s AND e.decided_at IS NOT NULL "
            "  AND e.decided_at > now() - interval '30 days' "
            "ORDER BY e.id DESC LIMIT 20", (uid,)):
        loc = "/".join(str(b) for b in [r["alias"], r["database_name"]] if b) or "a target"
        granted = r["status"] == "approved"
        items.append({"id": f"er{r['id']}", "kind": "endpoint",
                      "title": "Endpoint provisioned" if granted else "Access request declined",
                      "body": (f"Your access request for {loc} was granted." if granted
                               else f"Your access request for {loc} was declined."),
                      "createdAt": mapping.iso(r["decided_at"])})
    for r in db.fetch_all(
            "SELECT id, actor_name, details, created_at FROM audit_log "
            "WHERE action = 'kill_switch_set' "
            "  AND created_at > now() - interval '30 days' "
            "ORDER BY id DESC LIMIT 6"):
        d = r["details"] if isinstance(r["details"], dict) else {}
        on = bool(d.get("enabled"))
        who = r["actor_name"] or "an admin"
        items.append({"id": f"kill{r['id']}", "kind": "kill",
                      "title": "Execution paused" if on else "Execution resumed",
                      "body": (f"The fleet-wide kill switch was engaged by {who}." if on
                               else f"The fleet-wide kill switch was lifted by {who}."),
                      "createdAt": mapping.iso(r["created_at"])})
    items.sort(key=lambda x: x["createdAt"] or "", reverse=True)
    return items[:50]


@router.get("/notifications")
def notifications_feed(claims: dict = Depends(deps.current_user)):
    """Developer notification feed, newest first, with per-item read state."""
    deps.require_whitelisted(claims)
    uid = claims["sub"]
    items = _derive_notifications(uid)
    if items:
        read = {r["notif_id"] for r in db.fetch_all(
            "SELECT notif_id FROM web_notification_reads "
            "WHERE slack_user_id = %s AND notif_id = ANY(%s)",
            (uid, [i["id"] for i in items]))}
        for i in items:
            i["read"] = i["id"] in read
    return {"notifications": items}


class NotifReadIn(BaseModel):
    ids: list[str] | None = None
    all: bool = False


@router.post("/notifications/read")
def notifications_read(body: NotifReadIn, claims: dict = Depends(deps.current_user)):
    """Mark notifications read ({ids:[...]} or {all:true}). Idempotent."""
    deps.require_whitelisted(claims)
    uid = claims["sub"]
    ids = [str(i)[:64] for i in (body.ids or [])][:200]
    if body.all:
        ids = [i["id"] for i in _derive_notifications(uid)]
    if ids:
        with db.transaction() as cur:
            cur.executemany(
                "INSERT INTO web_notification_reads (slack_user_id, notif_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                [(uid, nid) for nid in ids])
    return {"ok": True, "read": len(ids)}


# ---- POST /queries/batch (one bundle = one approval round) -------------------
#
# Submit several queries as ONE bundle: a single Slack approval round for the
# whole set (mirrors `/sql batch`). Each item still validates, auto-approves,
# and executes on its own. Reuses the exact same core_submit validation +
# bundles + auto_approve + executor machinery as the bot — no parallel path.

@router.post("/queries/batch", status_code=201)
def submit_batch(body: BatchIn, request: Request,
                 claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    deps.block_if_password_change_required(claims)
    from .. import auto_approve, bundles, executor, teams
    uid = claims["sub"]
    name = claims.get("name") or uid
    client_ip, user_agent = _client_ctx(request)
    if not bundles.is_enabled():
        raise deps._error(409, "conflict", "Batch submission is disabled.")
    if len(body.items) > bundles.max_items():
        raise deps._error(422, "validation",
                          f"A batch can hold at most {bundles.max_items()} items.")

    sched_date = sched_time = None
    if body.schedule is not None:
        try:
            sched_date, sched_time = _schedule_parts(claims, body.schedule.runAt)
        except ValueError:
            raise deps._error(422, "validation", "Invalid schedule.runAt.")

    # Validate every item up front (all-or-nothing) with the SAME pipeline as a
    # single submit; the 404-for-everything enumeration guard mirrors POST /queries.
    preps: list[core_submit.Prepared] = []
    for idx, it in enumerate(body.items, start=1):
        t = _target_by_alias(it.connectionId)
        grant = teams.effective_grant_for_user(uid, t.id) if t is not None else None
        if t is None or grant is None or (not t.enabled and not admins.is_admin(uid)):
            raise deps._error(404, "not_found",
                              f"Unknown connection '{it.connectionId}' (item {idx}).")
        prep = core_submit.validate_submission(
            uid, name, target_server_id=t.id, database_name=it.databaseId,
            query=it.sql, justification=body.justification, wants_result=True,
            result_format="csv", schedule_date=sched_date, schedule_time=sched_time,
            origin="web", client_ip=client_ip, user_agent=user_agent)
        if isinstance(prep, core_submit.Rejection):
            status, code = _REJECTION_HTTP.get(prep.reason or prep.field, (422, "validation"))
            raise deps._error(status, code, f"Item {idx}: " + prep.message.replace("*", ""))
        preps.append(prep)

    # AUTH.md §5 — live employment check if any item is RW/DDL (Slack logins
    # only; local accounts use the enabled requester/admin row as liveness).
    if (any(p.required_mode in ("rw", "ddl") for p in preps)
            and claims.get("provider") == "slack"):
        if not deps.slack_employment_ok(uid):
            sessions.revoke_user(uid, "users.info failed before RW/DDL batch")
            raise deps._error(401, "unauthenticated", "Slack account verification failed.")

    sched_for = preps[0].sched_for
    # Phase-23 policy: a super-admin's own submissions auto-approve (all tiers).
    super_auto = admins.is_super_admin(uid)
    # Per-item grant-based auto-approve — cover NOW and (if scheduled) at run time.
    aa_grants: list[dict | None] = []
    for p in preps:
        g = auto_approve.effective_grant(uid, p.required_mode,
                                         target_server_id=p.target.id,
                                         database_name=p.database)
        if g is not None and sched_for is not None and auto_approve.effective_grant(
                uid, p.required_mode, target_server_id=p.target.id,
                database_name=p.database, at_time=sched_for) is None:
            g = None
        aa_grants.append(g)

    items = [bundles.BundleItem(
        target_server_id=p.target.id, target_alias=p.target.alias,
        database_name=p.database, query=p.query, wants_result=p.wants_result,
        result_format=p.result_format, explain_plan=p.explain_plan,
        risk_summary=p.risk_summary) for p in preps]

    with db.transaction() as cur:
        # origin="web" so the bundle items carry it — the executor's
        # result-routing gate then honors "answer on the channel it came
        # from" for batch results too, instead of leaking the CSV
        # to Slack under the slack-origin default.
        result = bundles.insert_bundle_with_items(
            cur, requester_slack_id=uid, requester_name=name,
            justification=body.justification, scheduled_for=sched_for,
            items=items, origin="web")
        for p, row, grant in zip(preps, result["item_rows"], aa_grants):
            auto = grant is not None or super_auto
            details = {
                "bundle_id": result["bundle_id"], "position": row.get("position"),
                "target_alias": p.target.alias, "database": p.database,
                "wants_result": p.wants_result, "auto_approved": auto,
                "origin": "web"}
            if client_ip:
                details["client_ip"] = client_ip
            if user_agent:
                details["user_agent"] = user_agent
            audit_mod.log_in(cur, row["id"], uid, name, "submitted", details)
            if not auto:
                continue
            new_status = "scheduled" if sched_for is not None else "approved"
            if grant is not None:
                decided_by = auto_approve.AUTO_DECIDED_BY
                decided_name = auto_approve.decided_by_name_for(grant)
                approve_action, approve_actor = "auto_approved", None
                approve_details = {"grant_id": grant["id"], "max_tier": grant["max_tier"]}
            else:   # super-admin full access (self-authorized)
                decided_by, decided_name = uid, f"{name} (super-admin)"
                approve_action, approve_actor = "auto_approved_super", decided_name
                approve_details = {"reason": "super-admin full access", "tier": p.required_mode}
            cur.execute(
                "UPDATE requests SET status = %s, decided_by_slack_id = %s, "
                " decided_by_name = %s, decision_reason = %s, decided_at = NOW() "
                "WHERE id = %s "
                "RETURNING id, requester_slack_id, requester_name, target_server_id, "
                "          database_name, query, wants_result, justification, status, "
                "          scheduled_for, decided_by_slack_id, decided_by_name, "
                "          bundle_id, position",
                (new_status, decided_by, decided_name, decided_name, row["id"]))
            fresh = cur.fetchone()
            if fresh is not None:
                for k, v in fresh.items():
                    row[k] = v
            audit_mod.log_in(cur, row["id"], decided_by, approve_actor,
                             approve_action, approve_details)

    bundle_id = result["bundle_id"]
    client = _bot_client()
    auto_rows = [row for row, g in zip(result["item_rows"], aa_grants) if (g is not None or super_auto)]
    pending = [row for row, g in zip(result["item_rows"], aa_grants) if (g is None and not super_auto)]
    # Auto-approved, non-scheduled items run now; scheduled ones are picked up
    # by the existing scheduler thread. Best-effort per item.
    if sched_for is None:
        for row in auto_rows:
            try:
                executor.submit(row, client)
            except Exception:
                log.exception("batch executor.submit failed for request %s", row["id"])
    # Pending (non-auto) items get ONE bundle DM to the admins.
    if pending and admins.list_active():
        try:
            from ..slack_app import notifications
            notifications.notify_admins_bundle(client, bundle_id)
        except Exception:
            log.exception("notify_admins_bundle failed for bundle %s", bundle_id)

    return {
        "bundleId": str(bundle_id),
        "items": [{"queryId": str(r["id"]),
                   "status": mapping.status_to_web(r["status"])}
                  for r in result["item_rows"]],
    }


# ---- POST /explain (read-only plan preview) ---------------------------------
#
# A NON-executing EXPLAIN (FORMAT JSON) for a single read-only statement,
# reusing the exact pre_flight machinery the Slack submit path uses (RO
# creds, SET TRANSACTION READ ONLY, pinned search_path, 2s statement
# timeout). No request row is created and nothing runs. Write/DDL are
# refused — their impact is shown as an estimate at submit time instead.

class ExplainIn(BaseModel):
    connectionId: str
    databaseId: str | None = None
    sql: str = Field(min_length=1, max_length=200_000)


class ClassifyIn(BaseModel):
    connectionId: str
    databaseId: str | None = None
    sql: str = Field(min_length=1, max_length=200_000)


@router.post("/classify")
def classify_query(body: ClassifyIn, claims: dict = Depends(deps.current_user)):
    """Authoritative tier verdict for the editor.

    The browser has its own keyword classifier so the tier chip isn't blank
    while you type, but it is a hint and it drifts: it does not know this
    target's engine, the user's grant, or the auto-approve windows. Rendering
    that hint as fact meant the UI could show a green "Run" for a statement the
    server classifies DDL, and — worse — could *block* submission of a legal
    read whose string literal contained a semicolon.

    So the client asks here after typing settles, and renders THIS answer:
    the same query_safety.analyze() the submit path uses, the same grant
    resolution, and the same auto-approve lookup. Read-only: nothing is
    written, nothing is submitted.
    """
    deps.require_whitelisted(claims)
    uid = claims["sub"]

    from .. import teams
    t = _target_by_alias(body.connectionId)
    grant = teams.effective_grant_for_user(uid, t.id) if t is not None else None
    # Same 404-for-everything enumeration guard as POST /queries and /explain.
    if t is None or grant is None or (not t.enabled and not admins.is_admin(uid)):
        raise deps._error(404, "not_found",
                          f"Unknown connection '{body.connectionId}'.")

    database = body.databaseId or t.default_database
    allowed = grant["allowed_databases"]
    if allowed is not None and database not in allowed:
        raise deps._error(404, "not_found",
                          f"Unknown database '{database}'.")

    sql = body.sql.strip()
    safety = query_safety.analyze(sql, engine=t.engine)
    required = safety.main_tier or "ro"

    # Does the user's grant on THIS database reach the required tier? This is
    # the check that made the client's guess dangerous in both directions.
    current_mode = teams.effective_mode_for_database(uid, t.id, database)
    rank = {"ro": 1, "rw": 2, "ddl": 3}
    exceeds = rank.get(required, 3) > rank.get(current_mode or "", 0)

    will_auto = False
    if not safety.blocked and not exceeds:
        if admins.is_super_admin(uid):
            will_auto = True
        else:
            from .. import auto_approve
            will_auto = auto_approve.effective_grant(
                uid, required, target_server_id=t.id, database_name=database,
            ) is not None

    return {
        "tier": required.upper(),
        "statements": len(safety.statements or []),
        "blocked": bool(safety.blocked),
        "blockers": list(safety.blockers or []),
        "warnings": list(safety.warnings or []),
        "destructive": bool(safety.is_destructive),
        "grantedTier": (current_mode or "").upper() or None,
        "tierExceedsGrant": exceeds,
        "willAutoApprove": will_auto,
        # Whether the submit path will demand a justification, answered exactly
        # as core_submit answers it — this endpoint exists so the editor never
        # has to guess, and it was guessing wrong in two directions: it said
        # DDL-only when the server requires one for RW as well, and it ignored
        # `will_auto` two lines above even though an auto-approved request has no
        # approver to read the text. A field built against the old value would
        # have appeared for the wrong statements and demanded prose nobody reads.
        #
        # Two answers because scheduling changes it. A scheduled request is never
        # exempt: its grant may expire before the run time, in which case
        # core_submit falls back to normal approval and the reason is needed
        # after all. The client knows whether the user has picked a schedule and
        # this endpoint does not, so it reports both and the UI picks.
        "requiresJustification":
            core_submit.needs_justification(required, will_auto),
        # Named for the question it answers: will a human read this? Design read
        # the old key correctly — with auto-approval off, the answer coincides
        # with "an approver will see it" — but the NAME said `scheduled`, and a
        # bundle is the other case that always meets an approver. A field whose
        # name and meaning coincide only by accident is one rename away from a
        # silent bug, so the meaning gets the name.
        "requiresJustificationWhenReviewed":
            core_submit.needs_justification(required, False),
        # Deprecated alias, one release, so a client mid-round does not break.
        "requiresJustificationWhenScheduled":
            core_submit.needs_justification(required, False),
    }


def _explain_nodes(root: dict) -> list[dict]:
    """Flatten an EXPLAIN plan tree into the flat, depth-tagged node list
    the web Plan view renders: [{d, op, detail, warn}]."""
    seq_threshold = cfg.get_int("risk_seq_scan_rows", 100000)
    out: list[dict] = []

    def walk(node: dict, depth: int) -> None:
        if not isinstance(node, dict):
            return
        op = node.get("Node Type", "?")
        rows = int(node.get("Plan Rows") or 0)
        rel, idx = node.get("Relation Name"), node.get("Index Name")
        bits: list[str] = []
        if idx:
            bits.append(f"using {idx}")
        if rel:
            bits.append(f"on {rel}")
        bits.append(f"rows={rows:,}")
        cost = node.get("Total Cost")
        if cost is not None:
            bits.append(f"cost={float(cost):.0f}")
        out.append({
            "d": depth, "op": op, "detail": " · ".join(bits),
            "warn": op == "Seq Scan" and rows >= seq_threshold,
        })
        for child in node.get("Plans", []) or []:
            walk(child, depth + 1)

    walk(root, 0)
    return out


def _explain_hints(query: str, analysis: dict) -> list[dict]:
    """Risk hints for the Plan view: plan-derived (seq scan / high cost)
    plus a few static SQL smells, in the design's {level, text} shape."""
    hints: list[dict] = []
    flags = analysis.get("flags", [])
    if "seq_scan_large" in flags and analysis.get("seq_scans"):
        rel, rows = max(analysis["seq_scans"], key=lambda x: x[1])
        hints.append({"level": "high",
                      "text": f"Seq scan on {rel} (~{pre_flight._fmt_int(rows)} rows) — no usable index"})
    if "high_cost" in flags:
        hints.append({"level": "med",
                      "text": f"High planner cost (size {analysis.get('cost_band', '?')})"})
    t = " ".join((query or "").lower().split())
    if "select *" in t:
        hints.append({"level": "med", "text": "SELECT * returns all columns — may include PII"})
    if "select" in t and "limit" not in t:
        hints.append({"level": "med", "text": "No LIMIT — result set may be large"})
    if "like '%" in t:
        hints.append({"level": "low", "text": "Leading-wildcard LIKE — cannot use an index"})
    if not hints:
        hints.append({"level": "low", "text": "No obvious risks detected"})
    return hints


def _explain_view(plan: list | None, query: str) -> dict | None:
    """Map EXPLAIN (FORMAT JSON) to the web Plan view contract:
    {plan: {planningMs, rows, scan, nodes}, hints}. None if unparseable."""
    if not plan or not isinstance(plan, list) or not isinstance(plan[0], dict):
        return None
    root = plan[0].get("Plan")
    if not isinstance(root, dict):
        return None
    planning = plan[0].get("Planning Time")
    analysis = pre_flight.analyze_plan(plan) or {}
    return {
        "plan": {
            "planningMs": round(float(planning), 2) if planning is not None else 0,
            "rows": int(root.get("Plan Rows") or 0),
            "scan": root.get("Node Type") or "Plan",
            "nodes": _explain_nodes(root),
        },
        "hints": _explain_hints(query, analysis),
    }


@router.post("/explain")
def explain_query(body: ExplainIn, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    uid = claims["sub"]

    from .. import teams
    t = _target_by_alias(body.connectionId)
    grant = teams.effective_grant_for_user(uid, t.id) if t is not None else None
    # Same 404-for-everything enumeration guard as POST /queries: an
    # unknown, disabled-to-non-admins, or ungranted connection all look
    # identical, so a user can't probe which aliases exist.
    if t is None or grant is None or (not t.enabled and not admins.is_admin(uid)):
        raise deps._error(404, "not_found",
                          f"Unknown connection '{body.connectionId}'.")

    sql = body.sql.strip()
    safety = query_safety.analyze(sql, engine=t.engine)
    if safety.blocked:
        raise deps._error(422, "validation", " ".join(safety.blockers)[:3000])

    import sqlglot
    try:
        stmts = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except Exception:
        stmts = [sql]
    if len(stmts) > 1:
        raise deps._error(422, "validation", "Preview one statement at a time.")
    explain_mode = query_safety.required_mode(sql, engine=t.engine)
    if explain_mode not in ("ro", "rw") or not pre_flight.is_explainable(sql):
        raise deps._error(422, "validation",
                          "Plan preview is for a single read-only or read-write "
                          "statement (not DDL).")
    database = body.databaseId or t.default_database
    allowed = grant["allowed_databases"]
    if allowed is not None and database not in allowed:
        raise deps._error(404, "not_found",
                          f"Unknown connection '{body.connectionId}'.")

    # An RW plan preview runs EXPLAIN with the RW credential (ANALYZE OFF, so
    # nothing executes) — allow it only for users whose grant covers RW ON THIS
    # DATABASE (not the target-wide max tier; see the cross-product note in teams.py): you can plan what
    # you could run.
    if explain_mode == "rw":
        db_mode = teams.effective_mode_for_database(uid, t.id, database)
        if db_mode not in ("rw", "ddl"):
            raise deps._error(403, "forbidden",
                              "A read-write plan preview needs a read-write grant.")

    ok, err, plan = pre_flight.explain(t.id, database, explain_mode, sql,
                                       summary=True, allow_write=True)
    if not ok:
        raise deps._error(422, "validation", err or "Could not plan the query.")
    view = _explain_view(plan, sql)
    if view is None:
        raise deps._error(503, "server_error",
                          "No plan produced (target unreachable — try again).")
    return view


# ---- GET /queries/:id ---------------------------------------------------------

def _own_request(request_id: int, uid: str) -> dict:
    row = db.fetch_one(
        "SELECT * FROM requests WHERE id = %s AND requester_slack_id = %s",
        (request_id, uid))
    if row is None:
        raise deps._error(404, "not_found", "No such query.")
    return row


@router.post("/queries/{request_id}/cancel")
def query_cancel(request_id: int, claims: dict = Depends(deps.current_user)):
    """Stop a query that is currently running.

    Allowed for the requester (it is their query) or any admin (it is their
    database). Anyone else gets the same 404 an unknown id would give, so this
    is not an existence oracle for other people's requests.

    The response distinguishes what actually happened, because "cancelled" and
    "we had to kill the connection" are different facts and the second one is
    worth seeing: a backend blocked writing results to a slow client ignores
    pg_cancel_backend entirely, so the escalation is not hypothetical.
    """
    deps.require_whitelisted(claims)
    uid = claims["sub"]
    row = db.fetch_one("SELECT * FROM requests WHERE id = %s", (request_id,))
    if row is None:
        raise deps._error(404, "not_found", "No such query.")
    if row["requester_slack_id"] != uid and not admins.is_admin(uid):
        raise deps._error(404, "not_found", "No such query.")
    # Two different jobs behind one button. Before execution there is no
    # backend to signal, so this is a withdrawal: close the row, leave the audit
    # line, and (for a pending one) take it off the admins' queue. Once it is
    # executing, stopping it means reaching the actual database connection.
    if row["status"] in cancellation.WITHDRAWABLE:
        if cancellation.withdraw(request_id, uid, claims.get("name")):
            return {"id": str(request_id), "stopped": True,
                    "outcome": "withdrawn",
                    "message": "Request withdrawn before it ran."}
        # Lost the race: it started executing (or somebody else closed it)
        # between the read above and the UPDATE. Re-read and fall through.
        row = db.fetch_one("SELECT * FROM requests WHERE id = %s", (request_id,))
        if row is None or row["status"] != "executing":
            raise deps._error(409, "conflict",
                              "That request is no longer waiting — reload to "
                              "see where it got to.")

    elif row["status"] != "executing":
        raise deps._error(409, "conflict",
                          "That query is not running any more.")

    outcome = cancellation.cancel(request_id, uid, claims.get("name"))
    stopped = outcome in (cancellation.CancelOutcome.CANCELLED,
                          cancellation.CancelOutcome.TERMINATED)
    return {
        "id": str(request_id),
        "stopped": stopped,
        "outcome": outcome,
        "message": {
            cancellation.CancelOutcome.CANCELLED:
                "Query cancelled.",
            cancellation.CancelOutcome.TERMINATED:
                "Query would not respond to a cancel, so its database "
                "connection was closed. It is stopped.",
            cancellation.CancelOutcome.NOT_RUNNING:
                "It finished before the cancel reached it.",
            cancellation.CancelOutcome.FAILED:
                "Could not reach the database to stop it — ask a DBA.",
        }[outcome],
    }


@router.get("/queries/{request_id}")
def query_status(request_id: int, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    row = _own_request(request_id, claims["sub"])
    audit_rows = db.fetch_all(
        "SELECT actor_slack_id, actor_name, action, created_at "
        "FROM audit_log WHERE request_id = %s ORDER BY id",
        (request_id,))
    return {
        "id": str(row["id"]),
        "status": mapping.status_to_web(row["status"]),
        "classification": mapping.classification_of(row["query"]),
        "approver": mapping.approver_label(row.get("decided_by_slack_id"),
                                           row.get("decided_by_name")),
        "approvedAt": mapping.iso(row.get("decided_at")),
        "scheduledFor": mapping.iso(row.get("scheduled_for")),
        "runMs": mapping.run_ms(row),
        "rowCount": row.get("row_count"),
        "audit": mapping.audit_entries(audit_rows, claims["sub"]),
        "messages": mapping.status_messages(row),
    }


# ---- results -------------------------------------------------------------------

def _result_file(row: dict) -> Path:
    path = row.get("csv_file_path")
    if not path:
        raise deps._error(404, "not_found", "No stored result for this query.")
    p = Path(path)
    if not p.is_file():
        raise deps._error(404, "not_found",
                          "Result file expired (results are kept for a limited time).")
    return p


def _dedup_cols(cols: list[str]) -> list[str]:
    """Make column names unique so a result with two same-named columns
    (e.g. `SELECT a.id, b.id FROM a JOIN b`) doesn't collapse to one key when
    each row is built as a dict (the second `id` silently overwrote
    the first). Duplicates get a ` (2)`, ` (3)`, … suffix in first-seen order.
    The CSV / XLSX downloads keep the raw header untouched — this only affects
    the JSON dict shape the grid consumes."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for c in cols:
        seen[c] = seen.get(c, 0) + 1
        out.append(c if seen[c] == 1 else f"{c} ({seen[c]})")
    return out


def _masked_pii_cols(row: dict, cols: list[str],
                     labels: list[str] | None = None) -> list[str]:
    """Which catalog columns were masked in the delivered result — the
    header dot hint. Uses the EXACT resolver the executor used
    (pii.exemption_decision), so db-wide / table-only exemptions and the
    skip_all case are honored instead of re-implemented (the old version
    mis-passed all exemption rows to _column_skips and ignored those
    cases). This mirrors executor.py's own masking decision. (Cells
    masked purely by content detectors in free-text columns are already
    redacted in the file; they just don't earn a column-header dot.)

    Matching runs on the RAW column names (`cols`) so the catalog resolves
    against real names; the returned names use `labels` (the deduped names
    the grid actually keys on) so a duplicate-named PII column still lines up.
    """
    labels = labels if labels is not None else cols
    if not pii.is_enabled():
        return []
    try:
        skip_all, pii_skip = pii.exemption_decision(
            row["target_server_id"], row["database_name"], row["query"], cols,
            principal_id=row["requester_slack_id"])
        pii_ns = pii.exemption_namescan(
            row["target_server_id"], row["database_name"], row["query"], cols,
            principal_id=row["requester_slack_id"])
    except Exception:
        log.exception("piiCols exemption check failed for request %s; "
                      "keeping dots on (fail-closed)", row["id"])
        skip_all, pii_skip, pii_ns = False, set(), set()
    if skip_all:
        return []
    # Soft (keep_value_scan) columns lose the header dot: the column-name mask
    # is off; only individual PII cells are content-masked (no column-wide dot).
    pii_map = pii.column_pii_map(cols, row["query"])
    return [labels[i] for i in sorted(pii_map)
            if i not in pii_skip and i not in pii_ns and i < len(labels)]


@router.get("/queries/{request_id}/result")
def query_result(request_id: int, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    row = _own_request(request_id, claims["sub"])
    if row["status"] != "completed":
        raise deps._error(409, "conflict",
                          f"Result not ready (status: {mapping.status_to_web(row['status'])}).")

    if not row.get("csv_file_path"):
        # Write/DDL — no result set; report affected rows.
        n = row.get("row_count")
        return {"kind": "affected", "affected": n,
                "message": f"{n if n is not None else '?'} row(s) affected.",
                "runMs": mapping.run_ms(row)}

    p = _result_file(row)
    if p.suffix.lower() != ".csv":
        raise deps._error(409, "conflict",
                          "Stored result is not CSV — download it instead "
                          f"(/api/queries/{request_id}/result.csv).")

    # A single result cell can exceed csv's default 128 KB field limit
    # (e.g. a big JSON/text column); the executor already stored it, so
    # reading must not blow up. Raise the field cap to the size ceiling.
    csv.field_size_limit(cfg.get_int("csv_size_mb_ceiling", 100) * 1024 * 1024)
    max_rows = cfg.get_int("web_result_max_rows", 1000)
    raw_cols: list[str] = []
    cols: list[str] = []
    rows: list[dict] = []
    truncated = False
    with p.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for i, rec in enumerate(reader):
            if i == 0:
                raw_cols = rec
                cols = _dedup_cols(rec)
                continue
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append(dict(zip(cols, rec)))

    return {
        "kind": "table",
        "cols": cols,
        "rows": rows,
        # Real per-column SQL types, captured from the driver's cursor
        # description at execution time (migration 083). The grid's header
        # tooltip used to guess these from a column-NAME map built off the
        # schema snapshot, which dropped any name found in two tables with
        # different types — so `id`, `user_id` and `created_at` never had one.
        # NULL for requests executed before 083; the grid falls back.
        "colTypes": row.get("result_column_types") or None,
        "piiCols": _masked_pii_cols(row, raw_cols, cols),
        "runMs": mapping.run_ms(row),
        "rowCount": row.get("row_count"),
        # Full row count so the grid can page the whole result via /rows,
        # not just the first page returned inline here.
        "total": row.get("row_count") if row.get("row_count") is not None else len(rows),
        "truncated": truncated or bool(row.get("truncated")),
    }


@router.get("/queries/{request_id}/rows")
def query_rows(request_id: int, offset: int = 0, limit: int = 100,
               claims: dict = Depends(deps.current_user)):
    """A page of a completed table result. Reads a window from the stored
    (already PII-masked) CSV so the grid pages large results without holding
    the whole set in memory. offset/limit are clamped; rows are dicts, the
    same shape as /result.rows."""
    deps.require_whitelisted(claims)
    row = _own_request(request_id, claims["sub"])
    if row["status"] != "completed" or not row.get("csv_file_path"):
        raise deps._error(409, "conflict", "No table result to page.")
    p = _result_file(row)
    if p.suffix.lower() != ".csv":
        raise deps._error(409, "conflict",
                          "This result isn't pageable in the grid — download it.")
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 2000))
    csv.field_size_limit(cfg.get_int("csv_size_mb_ceiling", 100) * 1024 * 1024)
    cols: list[str] = []
    out: list[dict] = []
    with p.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for i, rec in enumerate(reader):
            if i == 0:
                cols = _dedup_cols(rec)   # keep same-named columns distinct
                continue
            if (i - 1) < offset:      # (i-1) = zero-based data-row index
                continue
            if len(out) >= limit:
                break
            out.append(dict(zip(cols, rec)))
    return {"cols": cols, "rows": out, "offset": offset, "limit": limit}


@router.get("/queries/{request_id}/result.csv")
def query_result_csv(request_id: int, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    row = _own_request(request_id, claims["sub"])
    if row["status"] != "completed":
        raise deps._error(409, "conflict", "Result not ready.")
    p = _result_file(row)
    media = "text/csv" if p.suffix.lower() == ".csv" else \
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    audit_mod.log(request_id, claims["sub"], claims.get("name"),
                  "web_result_downloaded", {"file": p.name})
    return FileResponse(p, media_type=media,
                        filename=f"queryhub_result_{request_id}{p.suffix}")


@router.get("/queries/{request_id}/result.xlsx")
def query_result_xlsx(request_id: int, claims: dict = Depends(deps.current_user)):
    """Full result as a real streamed XLSX. If the stored artifact is already
    xlsx, serve it; otherwise convert the stored (already PII-masked) CSV using
    openpyxl's write-only workbook so a large result doesn't balloon memory."""
    deps.require_whitelisted(claims)
    row = _own_request(request_id, claims["sub"])
    if row["status"] != "completed":
        raise deps._error(409, "conflict", "Result not ready.")
    p = _result_file(row)
    xlsx_media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    audit_mod.log(request_id, claims["sub"], claims.get("name"),
                  "web_result_downloaded", {"format": "xlsx", "file": p.name})
    if p.suffix.lower() == ".xlsx":
        return FileResponse(p, media_type=xlsx_media,
                            filename=f"queryhub_result_{request_id}.xlsx")
    if p.suffix.lower() != ".csv":
        raise deps._error(409, "conflict",
                          "Stored result can't be exported as Excel — download it directly.")
    from openpyxl import Workbook
    csv.field_size_limit(cfg.get_int("csv_size_mb_ceiling", 100) * 1024 * 1024)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Result")
    with p.open(newline="", encoding="utf-8") as fh:
        for rec in csv.reader(fh):
            ws.append(rec)
    fd, tmp_path = tempfile.mkstemp(prefix=f"qhx_{request_id}_", suffix=".xlsx")
    os.close(fd)
    wb.save(tmp_path)
    # Stream the temp file, then delete it once the response is sent.
    return FileResponse(tmp_path, media_type=xlsx_media,
                        filename=f"queryhub_result_{request_id}.xlsx",
                        background=BackgroundTask(os.remove, tmp_path))


# ---- /scheduled (real scheduled queries, from `requests`) --------------------
#
# The genuine scheduled queries live in `requests` (status='scheduled' with a
# future scheduled_for), created by a submit carrying schedule.runAt — so the
# Scheduled panel reflects real pending runs, and cancelling one cancels the
# request so it never executes. No separate table needed.

@router.get("/scheduled")
def scheduled_list(claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    rows = db.fetch_all(
        "SELECT id, query, target_server_id, database_name, scheduled_for, created_at "
        "FROM requests WHERE requester_slack_id = %s AND status = 'scheduled' "
        "  AND scheduled_for > NOW() ORDER BY scheduled_for",
        (claims["sub"],))
    return {"scheduled": [mapping.scheduled_entry(r, _alias_of) for r in rows]}


@router.delete("/scheduled/{request_id}", status_code=204)
def scheduled_cancel(request_id: int, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    row = db.fetch_one(
        "UPDATE requests SET status = 'cancelled', decided_at = NOW(), "
        "  decision_reason = 'cancelled by requester (scheduled)' "
        "WHERE id = %s AND requester_slack_id = %s AND status = 'scheduled' "
        "RETURNING id", (request_id, claims["sub"]))
    if row is None:
        raise deps._error(404, "not_found", "No such scheduled query.")
    audit_mod.log(request_id, claims["sub"], claims.get("name"),
                  "cancelled", {"by": "requester", "phase": "scheduled"})


# ---- live status stream (WebSocket) ------------------------------------------
#
# The contract's recommended upgrade over 1.5s HTTP polling. The bot writes
# request/audit changes in a SEPARATE process, so this handler detects
# changes with a short server-side DB poll and pushes deltas over one
# persistent connection — the client no longer re-issues authenticated GETs
# every 1.5s. Auth happens on the handshake (session cookie); the frontend
# falls back to HTTP polling if the socket can't open.

_WS_POLL_SEC = 1.0
_WS_MAX_TICKS = 900          # ~15 min ceiling per socket
_WS_TERMINAL = {"failed", "rejected", "cancelled", "expired", "changes_requested"}


async def _ws_auth(websocket: WebSocket, request_id: int) -> str | None:
    """Authenticate a stream handshake from the session cookie. Returns the
    slack id on success; closes the socket and returns None otherwise."""
    # Origin first, and it has to live here rather than in the middleware: the
    # `@app.middleware("http")` security layer only sees http scope, so a
    # WebSocket handshake bypasses the CSRF check every other route gets.
    #
    # SameSite=Lax already stops a cross-site, script-initiated handshake from
    # carrying the session cookie in a current browser, so this is
    # defence-in-depth — the same reasoning the HTTP middleware states for
    # itself. It earns its place because that cookie attribute is otherwise the
    # ONLY thing between a page on another origin and a live status stream for
    # someone else's query, and a future change to the cookie flags would remove
    # it silently.
    if not deps.origin_is_same_site(websocket):
        await websocket.close(code=4403)
        return None
    token = websocket.cookies.get(deps.SESSION_COOKIE)
    claims = sessions.verify_access(token) if token else None
    if not claims:
        await websocket.close(code=4401)
        return None
    uid = claims["sub"]
    if not await asyncio.to_thread(sessions.session_alive, claims["sid"]):
        await websocket.close(code=4401)
        return None
    ok = await asyncio.to_thread(
        lambda: admins.is_admin(uid) or requesters.is_allowed(uid))
    if not ok:
        await websocket.close(code=4403)
        return None
    owns = await asyncio.to_thread(
        db.fetch_one,
        "SELECT 1 AS x FROM requests WHERE id = %s AND requester_slack_id = %s",
        (request_id, uid))
    if owns is None:
        await websocket.close(code=4404)
        return None
    return uid


@router.websocket("/queries/{request_id}/stream")
async def query_stream(websocket: WebSocket, request_id: int):
    uid = await _ws_auth(websocket, request_id)
    if uid is None:
        return
    await websocket.accept()
    last_status: str | None = None
    last_audit_n = 0
    try:
        for _ in range(_WS_MAX_TICKS):
            r = await asyncio.to_thread(
                db.fetch_one,
                "SELECT status, scheduled_for, executed_at, decided_by_slack_id, "
                "       decided_by_name FROM requests "
                "WHERE id = %s AND requester_slack_id = %s",
                (request_id, uid))
            if r is None:
                break

            web_status = mapping.status_to_web(r["status"])
            if web_status != last_status:
                last_status = web_status
                await websocket.send_json({
                    "type": "status", "id": str(request_id), "status": web_status,
                    "approver": mapping.approver_label(
                        r.get("decided_by_slack_id"), r.get("decided_by_name")),
                })

            arows = await asyncio.to_thread(
                db.fetch_all,
                "SELECT actor_slack_id, actor_name, action, created_at "
                "FROM audit_log WHERE request_id = %s ORDER BY id",
                (request_id,))
            entries = mapping.audit_entries(arows, uid)
            for e in entries[last_audit_n:]:
                await websocket.send_json({
                    "type": "audit", "id": str(request_id), "entry": e})
            last_audit_n = len(entries)

            # A scheduled query runs much later — don't hold the socket open.
            if (r.get("scheduled_for") and not r.get("executed_at")
                    and r["status"] in ("scheduled", "approved", "pending")
                    and r["scheduled_for"] > datetime.now(timezone.utc)):
                break
            if r["status"] == "completed":
                await websocket.send_json({
                    "type": "result_ready", "id": str(request_id)})
                break
            if r["status"] in _WS_TERMINAL:
                break

            await asyncio.sleep(_WS_POLL_SEC)
    except WebSocketDisconnect:
        return
    except Exception:
        log.exception("query_stream error for request %s", request_id)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
