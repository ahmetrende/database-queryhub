"""Passwords must not survive in request history.

A role-creation script carries the new password as a literal, and the whole
statement is stored so the request can be reviewed, audited and re-run. Four of
them sat in `requests.query` for up to twelve days while both accounts were
live — anyone who could read the column had a working login.

The masking is a trigger, not application code, because nineteen places across
five modules write a terminal status, two processes run the executor, and an
operator can UPDATE the row from psql. One point every writer goes through.

Timing was the design here: at submit the executor still had to send the real
statement, so the masking waited for a terminal state, and `awaiting_dba_manual`
was excluded because a human still had to run the statement by hand.

Migration 129 replaced that timing (2026-09-23: a password is not stored at
all). The statement is written masked, the executor reads an encrypted copy,
and the trigger masks on every write. These tests keep migration 100's file
honest; the current behaviour is pinned in test_query_secrets.py.
"""
import os
import re
from pathlib import Path

import pytest

MIGRATION = (Path(__file__).resolve().parent.parent
             / "migrations" / "100_scrub_query_secrets.sql")
SQL = MIGRATION.read_text(encoding="utf-8")

TERMINAL = ["completed", "failed", "cancelled", "rejected"]


def _trigger_body() -> str:
    return SQL[SQL.index("requests_scrub_secrets()"):SQL.index("DROP TRIGGER")]


def test_the_migration_installs_a_before_update_trigger():
    assert re.search(r"CREATE TRIGGER trg_requests_scrub_secrets\s+"
                     r"BEFORE UPDATE ON requests", SQL)


@pytest.mark.parametrize("status", TERMINAL)
def test_every_terminal_status_is_covered(status):
    assert f"'{status}'" in _trigger_body()


def test_a_later_migration_is_the_trigger_in_force():
    """100's exclusion of `awaiting_dba_manual` no longer holds: the DBA now sees
    the masked script and sets a new password. The latest migration defining
    the trigger function is the one that runs."""
    defining = sorted(f.name for f in MIGRATION.parent.glob("*.sql")
                      if "FUNCTION requests_scrub_secrets()" in f.read_text(encoding="utf-8"))
    assert defining[-1] in ("129_request_query_secret.sql",
                            "130_fix_scrub_trigger_status.sql")


def test_masking_twice_does_not_nest_the_marker():
    # Without this guard a second UPDATE would replace the marker's own quotes
    # and the column would slowly fill with REDACTED inside REDACTED.
    assert r"!~ '\*\*\*REDACTED\*\*\*'" in _trigger_body()
    assert SQL.count(r"!~ '\*\*\*REDACTED\*\*\*'") >= 2  # trigger + backfill


def test_the_backfill_only_touches_terminal_rows():
    backfill = SQL[SQL.index("UPDATE requests"):]
    assert "status IN ('completed', 'failed', 'cancelled', 'rejected')" in backfill


def test_the_keyword_is_what_anchors_the_match():
    """A bare literal must not be masked: `WHERE name = 'password123'` is data,
    not a credential. The pattern requires the PASSWORD keyword in front."""
    pattern = re.search(r"'\(\(\?:ENCRYPTED.*?'\)'", SQL, re.S)
    assert pattern, "the scrub pattern is not in the migration"
    assert "PASSWORD" in pattern.group(0)


# --- against a real database -------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                    reason="set QH_RUN_INTEGRATION=1 with a reachable control DB")
def test_the_trigger_masks_every_write_and_drops_the_copy_at_the_end():
    """Migration 129: a cleartext insert is masked on the way in, and the
    encrypted copy survives only while the request can still run."""
    from queryhub import db

    secret = "CREATE ROLE t_probe LOGIN PASSWORD 'Sup3rSecret-not-real-42';"
    with db.transaction() as cur:
        cur.execute("""INSERT INTO requests
            (requester_slack_id, requester_name, target_server_id, database_name,
             query, wants_result, status, required_tier, query_secret)
            VALUES ('U_TEST_PROBE', 'trigger probe', 1, 'queryhub', %s, false,
                    'approved', 'ddl', 'opaque-token') RETURNING id""", (secret,))
        rid = cur.fetchone()["id"]
    try:
        def row():
            return db.fetch_one("SELECT query, query_secret FROM requests WHERE id=%s", (rid,))

        assert "Sup3rSecret" not in row()["query"]      # masked on INSERT
        assert "PASSWORD '***REDACTED***'" in row()["query"]
        assert row()["query_secret"] == "opaque-token"  # approved: still runnable
        db.execute("UPDATE requests SET status='executing' WHERE id=%s", (rid,))
        assert row()["query_secret"] == "opaque-token"
        db.execute("UPDATE requests SET status='awaiting_dba_manual' WHERE id=%s", (rid,))
        assert row()["query_secret"] is None            # a DBA sets a new one
    finally:
        db.execute("DELETE FROM requests WHERE id=%s", (rid,))
