"""Cancel a running query at each tier, against a real PostgreSQL server.

PostgreSQL lets a session cancel or end another backend only when it has the
privileges of that backend's login role, or holds `pg_signal_backend`. The
unit tests in tests/test_cancellation.py check which login the cancel path
uses. Only a real server can show that this login can stop the query.

These tests make one throwaway non-superuser login per tier. Each test starts
`pg_sleep` as one of them, then calls the real cancel path. The control
database holds two disabled targets that point at the test server: one with a
credential for each tier, and one with the RO credential only.

The tests make the logins on the server in QH_TARGET_ADMIN_DSN, a superuser
conninfo. Without it, the control login makes them if it is a superuser. The
tests drop the logins at the end.
"""
import os
import secrets
import threading
import time

import psycopg
import pytest
from psycopg import sql

from queryhub import cancellation, db
from queryhub import config as cfg
from queryhub.cancellation import CancelOutcome
from queryhub.config import ENV
from queryhub.crypto import encrypt

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("QH_RUN_INTEGRATION"),
        reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

_LOGINS = {"ro": "qh_it_cancel_ro", "rw": "qh_it_cancel_rw",
           "ddl": "qh_it_cancel_ddl"}
# Stands in for the elevated role that a super-admin's DDL enters (SET ROLE).
_ELEVATED = "qh_it_cancel_elevated"
_REQUESTER = "U0AB12CD34"


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


def _drop_logins(admin) -> None:
    names = [*_LOGINS.values(), _ELEVATED]
    admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  " WHERE usename = ANY(%s)", (names,))
    deadline = time.monotonic() + 10
    while admin.execute("SELECT count(*) FROM pg_stat_activity "
                        " WHERE usename = ANY(%s)", (names,)).fetchone()[0]:
        assert time.monotonic() < deadline, "a test session did not end"
        time.sleep(0.1)
    for name in names:
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
            sql.Identifier(name)))


def _seed_target(alias: str, where: dict, creds: dict) -> int:
    """A disabled target that points at the test server. The tests keep it
    between runs and find it by its alias. They write the credentials again
    on each run, because each test session has its own master key (conftest)."""
    cols: dict = {"host": where["host"], "port": where["port"],
                  "default_database": where["dbname"], "secrets_provider": None,
                  "username": creds["ro"][0],
                  "password_encrypted": encrypt(creds["ro"][1])}
    for tier in ("rw", "ddl"):
        user, password = creds.get(tier, (None, None))
        cols[f"username_{tier}"] = user
        cols[f"password_{tier}_encrypted"] = encrypt(password) if password else None
    with db.transaction() as cur:
        cur.execute("SELECT id FROM target_servers WHERE alias = %s", (alias,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                f"INSERT INTO target_servers (alias, enabled, {', '.join(cols)}) "
                f"VALUES (%s, FALSE, {', '.join(['%s'] * len(cols))}) RETURNING id",
                (alias, *cols.values()))
            return cur.fetchone()["id"]
        cur.execute(
            f"UPDATE target_servers SET {', '.join(f'{c} = %s' for c in cols)} "
            f" WHERE id = %s", (*cols.values(), row["id"]))
        return row["id"]


@pytest.fixture(scope="module")
def server():
    admin = _admin()
    passwords = {tier: secrets.token_urlsafe(18) for tier in _LOGINS}
    _drop_logins(admin)
    for tier, name in _LOGINS.items():
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER PASSWORD {}").format(
            sql.Identifier(name), sql.Literal(passwords[tier])))
    # deploy/grant_readonly.sql gives the RO login pg_read_all_stats. So it
    # sees every session, and the server refuses it only at the signal.
    admin.execute(sql.SQL("GRANT pg_read_all_stats TO {}").format(
        sql.Identifier(_LOGINS["ro"])))
    admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(_ELEVATED)))
    admin.execute(sql.SQL("GRANT {} TO {}").format(
        sql.Identifier(_ELEVATED), sql.Identifier(_LOGINS["ddl"])))
    where = {"host": admin.info.host, "port": admin.info.port,
             "dbname": admin.info.dbname}
    every_tier = _seed_target("integration-cancel", where, {
        tier: (name, passwords[tier]) for tier, name in _LOGINS.items()})
    ro_only = _seed_target("integration-cancel-ro-only", where, {
        "ro": (_LOGINS["ro"], passwords["ro"])})
    try:
        yield {"admin": admin, "where": where, "passwords": passwords,
               "every_tier": every_tier, "ro_only": ro_only}
    finally:
        _drop_logins(admin)
        admin.close()


@pytest.fixture(autouse=True)
def plain_tcp(monkeypatch):
    """The test server is a local container without TLS. The fleet default,
    `require`, is not what these tests are about."""
    monkeypatch.setattr(cfg, "target_ssl_kwargs",
                        lambda host=None: {"sslmode": "prefer"})


class _Victim:
    """One statement that runs as the login of a tier, in a session that the
    executor would open: autocommit, with an application_name that names the
    request. A DDL session enters the elevated role first."""

    def __init__(self, server: dict, tier: str, request_id: int, seconds: int):
        w = server["where"]
        self.admin = server["admin"]
        self.login = _LOGINS[tier]
        self.conn = psycopg.connect(
            host=w["host"], port=w["port"], dbname=w["dbname"],
            user=self.login, password=server["passwords"][tier],
            autocommit=True, connect_timeout=10,
            application_name=f"queryhub req={request_id} by={_REQUESTER}")
        self.pid = self.conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        db.execute("UPDATE requests SET backend_pid = %s WHERE id = %s",
                   (self.pid, request_id))
        if tier == "ddl":
            self.conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(_ELEVATED)))
        self.result: str | None = None
        self.thread = threading.Thread(target=self._run, args=(seconds,),
                                       daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while self.admin.execute(
                "SELECT state FROM pg_stat_activity WHERE pid = %s",
                (self.pid,)).fetchone() != ("active",):
            assert time.monotonic() < deadline, "pg_sleep did not start"
            time.sleep(0.05)

    def _run(self, seconds: int) -> None:
        try:
            self.conn.execute("SELECT pg_sleep(%s)", (seconds,))
            self.result = "finished"
        except psycopg.errors.QueryCanceled:
            self.result = "cancelled"
        except Exception as e:  # noqa: BLE001 - the test reads the name
            self.result = type(e).__name__

    def close(self) -> None:
        """Ends the session, also when a failed test left the query running."""
        self.admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                           " WHERE pid = %s AND usename = %s",
                           (self.pid, self.login))
        self.thread.join(timeout=10)
        self.conn.close()


def _seed_request(target_id: int, tier: str, dbname: str, query: str) -> int:
    return db.insert_returning(
        "INSERT INTO requests (requester_slack_id, target_server_id, "
        "  database_name, query, status, executed_tier, executed_at) "
        "VALUES (%s, %s, %s, %s, 'executing', %s, NOW()) RETURNING id",
        (_REQUESTER, target_id, dbname, query, tier))["id"]


def _close_request(request_id: int) -> None:
    db.execute("UPDATE requests SET status = 'failed', completed_at = NOW(), "
               "  error_message = 'integration test cleanup' "
               " WHERE id = %s AND status = 'executing'", (request_id,))


@pytest.mark.parametrize("tier", ["ro", "rw", "ddl"])
def test_the_cancel_button_stops_a_query_at_every_tier(server, tier):
    rid = _seed_request(server["every_tier"], tier, server["where"]["dbname"],
                        "SELECT pg_sleep(30)")
    victim = _Victim(server, tier, rid, seconds=30)
    try:
        assert cancellation.cancel(rid, _REQUESTER, "Jordan Ray") \
            == CancelOutcome.CANCELLED
        victim.thread.join(timeout=15)
        assert victim.result == "cancelled"
        row = db.fetch_one("SELECT status::text AS st, error_message "
                           "  FROM requests WHERE id = %s", (rid,))
        assert row["st"] == "failed"
        assert "Jordan Ray" in row["error_message"]
    finally:
        victim.close()
        _close_request(rid)


def test_the_ro_login_cannot_stop_a_query_of_the_rw_login(server, caplog):
    """The failure before this change, kept as the fallback. The target has no
    RW credential, so the signal goes out with the RO login. That login sees
    the session but cannot signal it, and the query runs to its end."""
    rid = _seed_request(server["ro_only"], "rw", server["where"]["dbname"],
                        "SELECT pg_sleep(4)")
    victim = _Victim(server, "rw", rid, seconds=4)
    try:
        with caplog.at_level("WARNING", logger=cancellation.log.name):
            outcome = cancellation.stop_backend(rid)
        assert outcome == CancelOutcome.FAILED
        # SQLSTATE 42501. Its text changed in PostgreSQL 16, its name did not.
        assert "InsufficientPrivilege" in caplog.text
        victim.thread.join(timeout=15)
        assert victim.result == "finished"
    finally:
        victim.close()
        _close_request(rid)
