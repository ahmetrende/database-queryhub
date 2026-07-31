"""Append-only audit log."""
from __future__ import annotations

import json

from . import db

_INSERT_SQL = (
    "INSERT INTO audit_log (request_id, actor_slack_id, actor_name, action, details) "
    "VALUES (%s, %s, %s, %s, %s)"
)


def _params(
    request_id: int | None,
    actor_slack_id: str | None,
    actor_name: str | None,
    action: str,
    details: dict | None,
) -> tuple:
    return (
        request_id,
        actor_slack_id,
        actor_name,
        action,
        json.dumps(details) if details else None,
    )


def log(
    request_id: int | None,
    actor_slack_id: str | None,
    actor_name: str | None,
    action: str,
    details: dict | None = None,
) -> None:
    """Standalone audit insert. Prefer `log_in(cur, ...)` when paired
    with a state-changing UPDATE — the standalone form runs in its own
    transaction and can leave state out of sync if the audit insert
    fails."""
    db.execute(_INSERT_SQL, _params(request_id, actor_slack_id, actor_name, action, details))


def log_in(
    cur,
    request_id: int | None,
    actor_slack_id: str | None,
    actor_name: str | None,
    action: str,
    details: dict | None = None,
) -> None:
    """Audit insert reusing a caller-supplied cursor. The caller is
    expected to be inside `db.transaction()` (or holding any cursor whose
    transaction the caller will commit). Atomic with sibling state changes
    on the same cursor."""
    cur.execute(_INSERT_SQL, _params(request_id, actor_slack_id, actor_name, action, details))
