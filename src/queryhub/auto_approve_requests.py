"""Persistence for auto-approve window requests (the RO-burst nudge).

Mirrors `access_requests.py`: a small CRUD surface over the
`auto_approve_requests` table. The Slack UI lives in
`slack_app/ro_window.py`; the Bolt registrations in `slack_app/handlers.py`.
"""
from __future__ import annotations

import logging

from . import db

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
    return db.fetch_one(
        "UPDATE auto_approve_requests "
        "   SET status = %s, decided_by_slack_id = %s, decided_by_name = %s, "
        "       granted_id = %s, decided_at = NOW() "
        " WHERE id = %s AND status = 'pending' "
        "RETURNING id, requester_slack_id, target_server_id, database_name, "
        "          max_tier, window_minutes, status",
        (status, decided_by_slack_id, decided_by_name, granted_id, request_id),
    )


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


def valid_window(minutes: int) -> bool:
    from .slack_app import ro_window
    return ro_window.is_valid_window(minutes) or minutes in {d * 1440 for d in DAY_WINDOWS}


def submit_window(*, principal_id: str, name: str | None, target_id: int,
                  window_minutes: int, reason: str,
                  database_name: str | None = None, tier: str = "ro"):
    """Validate and persist one read-only window request.

    The rules the Slack modal always enforced, in one place so the web form
    enforces the same ones: the bot is not halted, the window is one of the
    offered lengths, the reason says something, the requester can reach the
    target (and the database, when one is named -- the approval carries it into
    the waiver), and there is not already a request pending for it.

    Returns (row, target). Notifying the admins is the caller's, because it
    needs a Slack client and the two surfaces hold theirs differently.
    """
    from . import auto_approve, core_submit, targets, teams
    if core_submit.kill_switch_on():
        raise WindowRequestRefused("reason", core_submit.kill_switch_message(), 503)
    tier = (tier or "ro").strip().lower()
    if tier == "ddl":
        raise WindowRequestRefused(
            "tier", "Schema changes are always reviewed and cannot be auto-approved.")
    if tier not in ("ro", "rw"):
        raise WindowRequestRefused("tier", "Pick RO or RW.")
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
    if tier == "rw":
        # A waiver covers what the access allows and no more, so asking to skip
        # review on writes needs write access to start with. Asked of the
        # resolver, per database when one is named.
        held = (teams.effective_mode_for_database(principal_id, target_id, db_scope)
                if db_scope else
                ((teams.effective_grant_for_user(principal_id, target_id) or {}).get("mode")))
        if _TIER_RANK.get((held or "").lower(), 0) < _TIER_RANK["rw"]:
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
    reached; zero means nobody can decide it."""
    from . import admins
    from .slack_app import notifications, ro_window
    blocks = ro_window.admin_dm_blocks(row, alias)
    fallback = (f"{(row.get('max_tier') or 'ro').upper()} auto-approve window request "
                f"from <@{row['requester_slack_id']}> for {alias} (#{row['id']})")
    reached = 0
    for a in admins.list_active():
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
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message, self.status = message, status


def decide_window(request_id: int, *, approve: bool, actor_id: str,
                  actor_name: str | None) -> dict:
    """Approve or decline a pending window request -- the Slack card and the web
    admin screen, one rulebook.

    Approving writes the waiver with its window starting NOW, at the decision,
    not at the ask. A scoped admin is held to the same tier, target and team
    scope as a query approval, because the waiver hands out exactly that. Two
    admins deciding at once: the second finds the request already decided.
    Returns {status, request, grant_id?, alias}.
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
    if not approve:
        decided = decide(request_id, status="rejected", decided_by_slack_id=actor_id,
                         decided_by_name=actor_name)
        if decided is None:
            raise WindowDecisionRefused("Already decided.")
        db.execute(
            "INSERT INTO audit_log (actor_slack_id, actor_name, action, details) "
            "VALUES (%s, %s, 'auto_approve_window_rejected', %s::jsonb)",
            (actor_id, actor_name, json.dumps({"request_id": request_id,
                                               "user": decided["requester_slack_id"]})))
        return {"status": "rejected", "request": req, "alias": alias}
    if not admins.can_approve(actor_id, {"required_tier": req["max_tier"],
                                         "target_server_id": req["target_server_id"],
                                         "requester_slack_id": req["requester_slack_id"]}):
        raise WindowDecisionRefused(
            "That request is outside your admin scope. Ask an admin with broader scope.", 403)
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
                "RETURNING id",
                (req["requester_slack_id"], req["max_tier"], req["target_server_id"],
                 auto_approve.normalise_scope(req["database_name"]), req["window_minutes"],
                 f"window request #{request_id}: {req['reason']}", actor_id))
            grant_id = cur.fetchone()["id"]
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
                    "max_tier": req["max_tier"], "window_minutes": req["window_minutes"]})))
        conn.commit()
    return {"status": "approved", "request": req, "grant_id": grant_id, "alias": alias}


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
