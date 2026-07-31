"""Allowlist of Slack users who can invoke /sql. Replaces the
REQUESTER_ALLOWLIST env var.

Rules (fail CLOSED):
  - Admins always pass (managed in the `admins` table separately) — this also
    bootstraps a fresh install: seed the first admin, no open window needed.
  - Otherwise ONLY users with an enabled `requesters` row are allowed.
  - An empty requesters table grants NO ONE access (it used to open the bot to
    every workspace user; that was a fail-open footgun if the table ever
    emptied). Access to the SQL gateway never widens on its own.

Emails are populated lazily by `profile_sync.maybe_backfill_email`.
"""
from __future__ import annotations

from . import admins, db


def is_allowed(principal_id: str) -> bool:
    # Admins always pass — this is also how a fresh install bootstraps (seed
    # the first admin directly), so the allowlist can safely fail CLOSED.
    if admins.is_admin(principal_id):
        return True
    # Fail CLOSED: only an explicitly-enabled requester gets in. An empty
    # requesters table does NOT grant everyone access. (It used to — "empty
    # allowlist == open mode" — which is a fail-open footgun: if the table is
    # ever emptied by accident, a bulk disable, or a bad cleanup, the whole
    # web app AND Slack /sql would open to every workspace user. Access to a
    # production SQL gateway must never widen on its own.)
    found = db.fetch_one(
        "SELECT 1 FROM requesters "
        "WHERE slack_user_id = %s AND enabled = TRUE LIMIT 1",
        (principal_id,),
    )
    return found is not None


def bypasses_team_grants(principal_id: str) -> bool:
    """True if the user's `requesters` row has `bypass_team_grants=TRUE`.
    Such users see every enabled target regardless of team_target_grants
    (mirrors admin visibility) but stay non-admin (cannot approve/reject)."""
    if not principal_id:
        return False
    row = db.fetch_one(
        "SELECT 1 FROM requesters "
        "WHERE slack_user_id = %s AND enabled = TRUE AND bypass_team_grants = TRUE "
        "LIMIT 1",
        (principal_id,),
    )
    return row is not None


def open_request_count(principal_id: str) -> int:
    """Number of in-flight requests for this user — pending admin review,
    awaiting scheduled execution, or currently executing. Used by the
    per-user rate limit so a single requester (or compromised account)
    can't fill the queue. Completed/failed/rejected/cancelled don't
    count."""
    row = db.fetch_one(
        "SELECT count(*) AS n FROM requests "
        "WHERE requester_slack_id = %s "
        "  AND status IN ('pending', 'changes_requested', 'approved', "
        "                 'scheduled', 'executing')",
        (principal_id,),
    )
    return row["n"] if row else 0


def get(principal_id: str) -> dict | None:
    return db.fetch_one(
        "SELECT slack_user_id, email, name, enabled, added_at, added_by "
        "FROM requesters WHERE slack_user_id = %s",
        (principal_id,),
    )
