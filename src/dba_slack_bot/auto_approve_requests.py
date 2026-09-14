"""Persistence for auto-approve window requests (the RO-burst nudge).

Mirrors `access_requests.py`: a small CRUD surface over the
`auto_approve_requests` table. The Slack UI lives in
`slack_app/ro_window.py`; the Bolt registrations in `slack_app/handlers.py`.
"""
from __future__ import annotations

from . import db


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
