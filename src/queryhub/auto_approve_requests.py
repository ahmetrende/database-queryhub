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


def submit_window(*, principal_id: str, name: str | None, target_id: int,
                  window_minutes: int, reason: str,
                  database_name: str | None = None):
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
    from .slack_app import ro_window
    if core_submit.kill_switch_on():
        raise WindowRequestRefused("reason", core_submit.kill_switch_message(), 503)
    if not ro_window.is_valid_window(window_minutes):
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
    row = create(principal_id=principal_id, name=name, target_server_id=target_id,
                 database_name=db_scope, max_tier="ro",
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
    fallback = (f"RO auto-approve window request from <@{row['requester_slack_id']}> "
                f"for {alias} (#{row['id']})")
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
