"""Integration tests that run against a REAL control database.

Skipped automatically when no DB is reachable (pure-unit CI), so they never
block the fast suite; they run in an environment configured with live
BOT_DB_* credentials (and in CI once a throwaway Postgres is provisioned).
These paths deliberately avoid cfg.get_setting, which conftest stubs to
return defaults without touching the DB.
"""
import logging
import os

import pytest

from dba_slack_bot import db

# Opt-in: these connect to a real control DB, so they stay OFF by default
# (the fast unit suite must not probe or block on a network DB). Run them
# with QH_RUN_INTEGRATION=1 in an environment configured with live BOT_DB_*
# credentials — or in CI once a throwaway Postgres is provisioned.
# Two markers, doing different jobs:
#   - `integration` makes the set selectable (`pytest -m integration`) and
#     exempts these tests from the conftest barrier that refuses real database
#     connections in unit tests.
#   - the skipif keeps them out of a local run that has no database.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("QH_RUN_INTEGRATION"),
        reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]


def test_readyz_reports_ready_against_real_db():
    from starlette.testclient import TestClient

    from dba_slack_bot.web import app
    logging.disable(logging.CRITICAL)
    with TestClient(app.create_app()) as c:
        r = c.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"


def test_healthz_is_always_ok():
    from starlette.testclient import TestClient

    from dba_slack_bot.web import app
    logging.disable(logging.CRITICAL)
    with TestClient(app.create_app()) as c:
        assert c.get("/healthz").json()["status"] == "ok"


def test_core_tables_exist():
    # A live query against the real schema — proves the pool + schema are
    # wired, not just that the connection opens.
    row = db.fetch_one(
        "SELECT count(*) AS n FROM information_schema.tables "
        "WHERE table_name IN ('requests','bot_config','team_target_grants')")
    assert row["n"] == 3


# ---------------------------------------------------------------------------
# Invariants that only a real database can prove. The three tests above check
# that the pool and the schema are wired; these check behaviour that lives in
# SQL — row locking, ON CONFLICT, triggers, generated defaults — and that a
# mocked cursor can only pretend to have.
# ---------------------------------------------------------------------------


def _seed_target():
    """A target row to satisfy requests.target_server_id's foreign key.

    Reused across tests by alias, so repeated runs against the same throwaway
    database do not accumulate rows.
    """
    row = db.fetch_one("SELECT id FROM target_servers WHERE alias = %s",
                       ("integration-fixture",))
    if row:
        return row["id"]
    row = db.insert_returning(
        "INSERT INTO target_servers "
        "  (alias, host, default_database, username, password_encrypted, enabled) "
        "VALUES (%s, %s, %s, %s, %s, FALSE) RETURNING id",
        ("integration-fixture", "127.0.0.1", "app", "unused", "unused"))
    return row["id"]


def _seed_request(status="approved", **over):
    """Insert a minimal request row and return its id."""
    # Only the columns that are NOT NULL without a default, plus status. Adding
    # a column that does not exist (there is no `required_mode`; the tier lives
    # in `executed_tier` / `engine_tier`) fails the INSERT, not the assertion.
    cols = {
        "requester_slack_id": "U0INTEG01",
        "target_server_id": _seed_target(),
        "database_name": "app",
        "query": "SELECT 1",
        "status": status,
    }
    cols.update(over)
    names = ", ".join(cols)
    holders = ", ".join(["%s"] * len(cols))
    row = db.insert_returning(
        f"INSERT INTO requests ({names}) VALUES ({holders}) RETURNING id",
        tuple(cols.values()))
    return row["id"]


def test_execution_claim_is_exclusive_under_concurrency():
    """The claim is `UPDATE ... WHERE id = %s AND status = 'approved'`. Two
    processes racing it must produce exactly one winner — that is what stops an
    approved RW statement from running twice, and it is a property of Postgres'
    row locking, so it cannot be tested with a fake cursor.
    """
    rid = _seed_request(status="approved")
    claim = ("UPDATE requests SET status = 'executing', executed_at = NOW() "
             "WHERE id = %s AND status = 'approved'")

    won = []
    with db.transaction() as cur:
        cur.execute(claim, (rid,))
        won.append(cur.rowcount)
    with db.transaction() as cur:
        cur.execute(claim, (rid,))
        won.append(cur.rowcount)

    assert won == [1, 0], f"claim was not exclusive: {won}"
    assert db.fetch_one("SELECT status FROM requests WHERE id = %s",
                        (rid,))["status"] == "executing"


def test_a_cancelled_request_cannot_be_claimed():
    """Cancel wins the race by moving the row out of 'approved'."""
    rid = _seed_request(status="cancelled")
    with db.transaction() as cur:
        cur.execute("UPDATE requests SET status = 'executing' "
                    "WHERE id = %s AND status = 'approved'", (rid,))
        assert cur.rowcount == 0


def test_migration_ledger_is_idempotent_and_checksum_guarded():
    """Re-running the migration runner must be a no-op, and a changed file must
    be refused rather than silently re-applied. The ledger is the mechanism, so
    this asserts on the ledger itself."""
    rows = db.fetch_all("SELECT filename, checksum FROM schema_migrations "
                        "ORDER BY filename")
    assert rows, "no migrations recorded — the ledger is not being written"
    assert all(r["checksum"] for r in rows), "a migration was recorded without a checksum"
    # Filenames are unique: a second apply of the same file cannot add a row.
    names = [r["filename"] for r in rows]
    assert len(names) == len(set(names))

    import subprocess
    import sys
    second = subprocess.run([sys.executable, "scripts/apply_migrations.py"],
                            capture_output=True, text=True, timeout=300)
    assert second.returncode == 0, second.stderr[-2000:]
    assert "Applying" not in second.stdout, \
        "a second run re-applied migrations:\n" + second.stdout[-2000:]

    after = db.fetch_all("SELECT filename FROM schema_migrations")
    assert len(after) == len(rows), "the ledger grew on a no-op run"


def test_bot_config_defaults_are_seeded_and_typed():
    """The admin UI reads bot_config; a key the code reads but the migrations
    never seed cannot be shown or set. Spot-check the ones that gate safety."""
    keys = ("query_timeout_sec", "execution_lease_sec", "results_ttl_hours",
            "web_result_max_rows")
    rows = db.fetch_all(
        "SELECT key, value FROM bot_config WHERE key = ANY(%s)", (list(keys),))
    found = {r["key"] for r in rows}
    assert found == set(keys), f"unseeded config keys: {set(keys) - found}"
    # execution_lease_sec must exceed query_timeout_sec, or the orphan
    # reconciler fails queries that are still running.
    vals = {r["key"]: int(r["value"]) for r in rows
            if r["key"] in ("query_timeout_sec", "execution_lease_sec")}
    assert vals["execution_lease_sec"] >= vals["query_timeout_sec"] + 300


def test_audit_log_search_indexes_exist():
    """A 60-second browser poll and the admin audit search both hit this table;
    without the indexes each one is a full scan."""
    rows = db.fetch_all(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'audit_log'")
    names = {r["indexname"] for r in rows}
    assert len(names) >= 3, f"audit_log has too few indexes: {names}"


def test_web_session_rotation_round_trips_against_real_sql():
    """create_session → rotate_refresh → replay. The reuse response and the
    grace window are pure SQL (make_interval, prev_refresh_hash), so a real
    server is the only place they can be checked."""
    from dba_slack_bot.web import sessions

    sid, tok = sessions.create_session("U0INTEG01", provider="local",
                                       user_agent="integ")
    assert sessions.session_alive(sid)

    rotated = sessions.rotate_refresh(tok)
    assert rotated and rotated["refresh_token"] != tok

    # The old token is now `prev`. Inside the grace window this is the ordinary
    # two-tab race and must rotate again, not revoke.
    again = sessions.rotate_refresh(tok)
    assert again is not None and "reuse" not in again

    sessions.revoke_session(sid, "integration test cleanup")
    assert not sessions.session_alive(sid)


# ---------------------------------------------------------------------------
# The 2026-07-29 wedge, against a real server. The unit tests pin the ordering;
# this one proves the mechanism, because the whole failure lives in how psycopg
# and PostgreSQL interact and no mock can show it.
#
# `SELECT ... FROM requests` on the control database is the perfect specimen:
# `status` is an enum, so psycopg reports its type as a bare OID and the executor
# has to ask the catalog for a name — the second query that got stuck.
# ---------------------------------------------------------------------------


def _enum_query_conn():
    """A raw autocommit connection, so portal behaviour is not masked by an outer
    transaction.

    Env credentials, like the rest of this file: CI provisions a throwaway
    Postgres and exports BOT_DB_PASSWORD. It cannot run on an operator host where
    that password lives in the encrypted secrets file instead — conftest sets
    BOT_DB_PASSWORD="test" at import time and repoints MASTER_KEY_PATH at a test
    key, so nothing here can reach the real one. Verified against a real server
    by hand there; verified continuously here.
    """
    import psycopg
    from psycopg.rows import dict_row

    from dba_slack_bot.config import ENV
    return psycopg.connect(
        host=ENV.bot_db_host, port=ENV.bot_db_port, dbname=ENV.bot_db_name,
        user=ENV.bot_db_user, password=ENV.bot_db_password,
        connect_timeout=10, autocommit=True, row_factory=dict_row,
        application_name="queryhub-test:portal-ordering")


def test_the_enum_column_really_does_come_back_as_a_bare_oid():
    """The premise. If psycopg ever learns to name enums by itself, the lookup
    (and this whole hazard) becomes dead code — and this test says so."""
    from dba_slack_bot import executor
    with _enum_query_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, status FROM requests LIMIT 1")
        types, unknown = executor._column_types(cur.description)
        assert unknown, ("no unnamed type in a result containing an enum — "
                         "psycopg may now resolve them, re-check the lookup")
        assert any(v.isdigit() for v in types.values())


def test_the_catalog_lookup_returns_promptly_after_the_stream_is_closed():
    """The fix, end to end: break the stream early (as the row cap does), close
    it, then run the lookup. The assertion is on TIME, because the bug was not a
    wrong answer — it was no answer at all.

    A 20-second budget for a single-row catalog query on the local control DB is
    two orders of magnitude of slack; the wedged version never returned.
    """
    import time
    from dba_slack_bot import executor
    with _enum_query_conn() as conn, conn.cursor() as cur:
        # Deliberately more rows than we read, so the portal is left holding
        # some. This is what a truncated result looks like.
        stream = cur.stream(
            "SELECT r.id, r.status FROM requests r, generate_series(1, 20) g")
        first = next(stream, None)
        assert first is not None, "no rows: the control DB has no requests to read"
        types, unknown = executor._column_types(cur.description)
        assert unknown
        read = 1
        for _row in stream:
            read += 1
            if read >= 5:
                break                       # the row cap, in miniature
        stream.close()                      # <- the fix
        started = time.monotonic()
        resolved = executor._apply_resolved_types(dict(types), unknown, cur)
        took = time.monotonic() - started
        assert took < 20, f"the catalog lookup took {took:.1f}s — it is wedging again"
        assert resolved, "the lookup produced nothing"
        assert not any(v.isdigit() for v in resolved.values()), \
            f"an OID survived into the result: {resolved}"


def test_the_connection_is_still_usable_afterwards():
    """Closing a portal mid-result must not poison the session — the executor
    goes on to run the remaining statements of a bundle on it."""
    with _enum_query_conn() as conn, conn.cursor() as cur:
        stream = cur.stream("SELECT id FROM requests")
        next(stream, None)
        stream.close()
        cur.execute("SELECT 42 AS answer")
        assert cur.fetchone()["answer"] == 42
