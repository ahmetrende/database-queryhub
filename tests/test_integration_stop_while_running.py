"""A Stop on a query that the real executor runs, against a real PostgreSQL server.

Before PR 347, a Stop on Postgres read as a statement timeout. The executor
thread got QueryCanceled before `cancellation.cancel` marked the row, and it
wrote the timeout text. The audit had no `cancelled_running` row. The unit
tests in tests/test_stop_reads_as_stop.py pin the fix with fakes. This test
runs the whole path:

1. `executor._run` claims an approved request and runs `pg_sleep` on the
   target as the RO login.
2. `cancellation.cancel` stops it, as the Stop button does.
3. The request fails with the stop note, and the audit has one
   `cancelled_running` row.

The target is the test server itself. The test makes one throwaway login and
one enabled target, so it runs only against a control database on this host.
"""
import os
import secrets
import threading
import time
from unittest import mock

import psycopg
import pytest
from psycopg import sql

from queryhub import cancellation, db, executor
from queryhub import config as cfg
from queryhub.cancellation import CancelOutcome
from queryhub.config import ENV
from queryhub.crypto import encrypt

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("QH_RUN_INTEGRATION"),
        reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
    pytest.mark.skipif(
        ENV.bot_db_host not in ("127.0.0.1", "localhost", "::1"),
        reason="enables a target and runs pg_sleep on it: runs only on a "
               "scratch control database on this host"),
]

_LOGIN = "qh_it_stop_ro"
_ALIAS = "integration-stop-while-running"
_REQUESTER = "U0ITSTOP01"
_STOPPER = ("U0ITSTOP02", "Jordan Ray")
_SLEEP = 8          # below the pg_sleep limit of ast_safety (10 s)


def _admin():
    dsn = os.environ.get("QH_TARGET_ADMIN_DSN")
    if dsn:
        return psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    conn = psycopg.connect(
        host=ENV.bot_db_host, port=ENV.bot_db_port, dbname=ENV.bot_db_name,
        user=ENV.bot_db_user, password=ENV.bot_db_password, autocommit=True,
        connect_timeout=10, **ENV.bot_db_ssl_kwargs())
    row = conn.execute(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
    if not (row and row[0]):
        conn.close()
        pytest.skip("the control login cannot make roles: set "
                    "QH_TARGET_ADMIN_DSN to a superuser conninfo")
    return conn


def _drop_login(admin) -> None:
    admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  " WHERE usename = %s", (_LOGIN,))
    deadline = time.monotonic() + 10
    while admin.execute("SELECT count(*) FROM pg_stat_activity WHERE usename = %s",
                        (_LOGIN,)).fetchone()[0]:
        assert time.monotonic() < deadline, "a test session did not end"
        time.sleep(0.1)
    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(_LOGIN)))


def _delete_audit_rows(admin) -> None:
    """The split runtime login may only add to audit_log. So the superuser
    deletes the audit rows of this test."""
    admin.execute("DELETE FROM audit_log WHERE request_id IN "
                  "(SELECT id FROM requests WHERE requester_slack_id = %s)",
                  (_REQUESTER,))


def _wipe(admin) -> None:
    """Delete every row of this person and this target, in both access models."""
    t = [r["id"] for r in db.fetch_all(
        "SELECT id FROM target_servers WHERE alias = %s", (_ALIAS,))]
    _delete_audit_rows(admin)
    db.execute("DELETE FROM requests WHERE requester_slack_id = %s", (_REQUESTER,))
    db.execute("DELETE FROM user_target_grants WHERE slack_user_id = %s", (_REQUESTER,))
    db.execute("DELETE FROM requesters WHERE slack_user_id = %s", (_REQUESTER,))
    p = [r["principal_id"] for r in db.fetch_all(
        "SELECT principal_id FROM principal_identity "
        " WHERE provider = 'slack' AND external_id = %s", (_REQUESTER,))]
    db.execute("DELETE FROM access_grant WHERE principal_id = ANY(%s) "
               "   OR target_id = ANY(%s)", (p, t))
    db.execute("DELETE FROM role_assignment WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal_setting WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM team_member WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal_identity WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal WHERE id = ANY(%s)", (p,))
    db.execute("DELETE FROM target_servers WHERE id = ANY(%s)", (t,))


@pytest.fixture(autouse=True)
def live_config(monkeypatch):
    """bot_config as production reads it: from the database, on every call.
    The test server is a local container without TLS."""
    def get_setting(key, default=None):
        row = db.fetch_one("SELECT value FROM bot_config WHERE key = %s", (key,))
        if row is None:
            if default is None:
                raise KeyError(key)
            return default
        return row["value"]
    monkeypatch.setattr(cfg, "get_setting", get_setting)
    monkeypatch.setattr(cfg, "target_ssl_kwargs",
                        lambda host=None: {"sslmode": "prefer"})


@pytest.fixture(scope="module")
def world():
    """One RO login, one enabled target on the test server, one requester with
    an RO grant on it. The access model is the production one."""
    admin = _admin()
    before = db.fetch_one("SELECT value FROM bot_config WHERE key = 'access_model_v2'")
    db.execute("UPDATE bot_config SET value = 'on' WHERE key = 'access_model_v2'")
    _wipe(admin)
    _drop_login(admin)
    password = secrets.token_urlsafe(18)
    admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER PASSWORD {}").format(
        sql.Identifier(_LOGIN), sql.Literal(password)))
    # deploy/grant_readonly.sql gives the RO login pg_read_all_stats.
    admin.execute(sql.SQL("GRANT pg_read_all_stats TO {}").format(sql.Identifier(_LOGIN)))
    where = {"host": admin.info.host, "port": admin.info.port,
             "dbname": admin.info.dbname}
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO target_servers (alias, host, port, default_database, "
            " secrets_provider, username, password_encrypted, enabled) "
            "VALUES (%s, %s, %s, %s, NULL, %s, %s, TRUE) RETURNING id",
            (_ALIAS, where["host"], where["port"], where["dbname"],
             _LOGIN, encrypt(password)))
        target_id = cur.fetchone()["id"]
        cur.execute("INSERT INTO requesters (slack_user_id, name, enabled) "
                    "VALUES (%s, 'IT Stop Requester', TRUE)", (_REQUESTER,))
        cur.execute("INSERT INTO user_target_grants (slack_user_id, target_server_id, mode) "
                    "VALUES (%s, %s, 'ro')", (_REQUESTER, target_id))
    try:
        yield {"admin": admin, "where": where, "target": target_id}
    finally:
        _wipe(admin)
        _drop_login(admin)
        db.execute("UPDATE bot_config SET value = %s WHERE key = 'access_model_v2'",
                   (before["value"],))
        admin.close()


def _approved_request(world: dict) -> dict:
    rid = db.insert_returning(
        "INSERT INTO requests (requester_slack_id, requester_name, target_server_id, "
        "  database_name, query, status) "
        "VALUES (%s, 'IT Stop Requester', %s, %s, %s, 'approved') RETURNING id",
        (_REQUESTER, world["target"], world["where"]["dbname"],
         f"SELECT pg_sleep({_SLEEP})"))["id"]
    return db.fetch_one("SELECT * FROM requests WHERE id = %s", (rid,))


def _wait_until_running(world: dict, rid: int) -> int:
    """The backend pid of the request, once its pg_sleep is active."""
    deadline = time.monotonic() + 15
    while True:
        row = db.fetch_one("SELECT status::text AS st, backend_pid FROM requests "
                           " WHERE id = %s", (rid,))
        if row["st"] == "executing" and row["backend_pid"]:
            state = world["admin"].execute(
                "SELECT state, query FROM pg_stat_activity WHERE pid = %s",
                (row["backend_pid"],)).fetchone()
            if state and state[0] == "active" and "pg_sleep" in state[1]:
                return row["backend_pid"]
        assert row["st"] in ("approved", "executing"), \
            f"the request ended before the stop: {row['st']}"
        assert time.monotonic() < deadline, "the executor did not start pg_sleep"
        time.sleep(0.05)


def test_a_stop_reads_as_a_stop_on_the_real_executor(world):
    request = _approved_request(world)
    rid = request["id"]
    client = mock.MagicMock()
    run = threading.Thread(target=executor._run, args=(request, client), daemon=True)
    started = time.monotonic()
    run.start()
    try:
        _wait_until_running(world, rid)
        outcome = cancellation.cancel(rid, *_STOPPER)
        run.join(timeout=20)
        assert not run.is_alive(), "the executor thread did not end"
        assert outcome == CancelOutcome.CANCELLED
        # The stop ended the query. pg_sleep did not run to its end.
        assert time.monotonic() - started < _SLEEP

        row = db.fetch_one("SELECT status::text AS st, error_message, "
                           "       cancel_requested_by FROM requests WHERE id = %s",
                           (rid,))
        assert row["st"] == "failed"
        assert row["error_message"] == "Cancelled by Jordan Ray while it was running."
        assert "statement timeout" not in row["error_message"]
        assert row["cancel_requested_by"] == _STOPPER[0]

        actions = [r["action"] for r in db.fetch_all(
            "SELECT action FROM audit_log WHERE request_id = %s ORDER BY id", (rid,))]
        assert actions.count("cancel_requested") == 1
        assert actions.count("cancelled_running") == 1
        assert actions.index("cancel_requested") < actions.index("cancelled_running")
        stop = db.fetch_one("SELECT actor_slack_id, actor_name FROM audit_log "
                            " WHERE request_id = %s AND action = 'cancelled_running'",
                            (rid,))
        assert (stop["actor_slack_id"], stop["actor_name"]) == _STOPPER
    finally:
        world["admin"].execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            " WHERE usename = %s", (_LOGIN,))
        run.join(timeout=10)
