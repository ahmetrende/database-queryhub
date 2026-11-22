"""Scheduler heartbeat: lets the web process see that a scheduler runs.

In production the scheduler loop runs in the Slack bot process. The web
process has no other way to know if it runs. The loop upserts one
`bot_config` row on each pass. The trigger on `bot_config` stamps
`updated_at` with the database clock, so the age needs no clock sync
between processes. Only the security keys are audited, so this row adds no
audit_log rows.
"""
from __future__ import annotations

import logging

from . import db

log = logging.getLogger(__name__)

HEARTBEAT_KEY = "scheduler_heartbeat_at"
DEFAULT_STALE_SEC = 180   # 3 x the default 60 second loop interval


def beat() -> None:
    """Record one scheduler pass. Never raises."""
    try:
        db.execute(
            "INSERT INTO bot_config (key, value, updated_at) "
            "VALUES (%s, 'alive', NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "  updated_at = NOW()",
            (HEARTBEAT_KEY,))
    except Exception:
        log.warning("scheduler heartbeat write failed", exc_info=True)


def check() -> dict:
    """Return {"status": "ok"|"stale", "age_sec": int|None}. Never raises."""
    limit = DEFAULT_STALE_SEC
    age = None
    try:
        row = db.fetch_one(
            "SELECT EXTRACT(EPOCH FROM (NOW() - updated_at)) AS age_sec "
            "FROM bot_config WHERE key = %s", (HEARTBEAT_KEY,))
        if row and row.get("age_sec") is not None:
            age = int(row["age_sec"])
    except Exception:
        log.warning("scheduler heartbeat read failed", exc_info=True)
    if age is not None and age <= limit:
        return {"status": "ok", "age_sec": age}
    return {"status": "stale", "age_sec": age}
