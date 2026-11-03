"""A change of an MCP switch leaves an audit_log row (migration 204).

The trigger of migration 066 records a change of a security key. The three
switches of the MCP door decide what data leaves QueryHub, so they are
security keys too. A switch that is not one of them changes with no row.
Needs a real control database: QH_RUN_INTEGRATION=1.
"""
from __future__ import annotations

import os

import pytest

from queryhub import db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]


def _value(key: str) -> str | None:
    row = db.fetch_one("SELECT value FROM bot_config WHERE key = %s", (key,))
    return row["value"] if row else None


def _rows_after(mark: int, key: str) -> list[dict]:
    return db.fetch_all(
        "SELECT action, details FROM audit_log "
        " WHERE id > %s AND action LIKE 'security_config_%%' AND details->>'key' = %s "
        " ORDER BY id", (mark, key))


def _mark() -> int:
    return db.fetch_one("SELECT COALESCE(MAX(id), 0) AS n FROM audit_log")["n"]


@pytest.mark.parametrize("key,changed", [("mcp_door_enabled", "on"),
                                         ("mcp_max_tier", "rw"),
                                         ("mcp_result_mode", "masked_rows")])
def test_a_change_of_an_mcp_switch_is_audited(key, changed):
    before = _value(key)
    assert before is not None, f"migrations 201 and 202 seed {key}"
    mark = _mark()
    try:
        db.execute("UPDATE bot_config SET value = %s WHERE key = %s", (changed, key))
        rows = _rows_after(mark, key)
        assert [r["action"] for r in rows] == ["security_config_changed"]
        assert rows[0]["details"]["old_value"] == before
        assert rows[0]["details"]["new_value"] == changed
    finally:
        db.execute("UPDATE bot_config SET value = %s WHERE key = %s", (before, key))


def test_a_switch_that_sends_no_data_is_not_audited():
    key = "mcp_rate_limit_per_minute"
    before = _value(key)
    mark = _mark()
    try:
        db.execute("UPDATE bot_config SET value = %s WHERE key = %s", ("61", key))
        assert _rows_after(mark, key) == []
    finally:
        db.execute("UPDATE bot_config SET value = %s WHERE key = %s", (before, key))
