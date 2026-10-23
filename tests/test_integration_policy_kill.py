"""PLA-1592 items U3, U4b and U4c against a real PostgreSQL server.

The unit tests (tests/test_rw_window_policy.py, tests/test_kill_switch_*.py)
pin the rules with fakes. Only a database shows that the SQL does what the
rules say:

* Migration 150 seeds `auto_approve_rw_requests` off. A second run keeps a
  value that an operator set.
* QueryHub refuses a RW window request while the switch is off. When it is on,
  the approved waiver ends when the window ends, and its audit row records that
  end. Only a super-admin decides a RW window on a production connection.
* The kill switch, set on through the web route, stops a running read and
  leaves a running write alone. The switch and its row commit first. A
  `kill_switch_cancelled` row after the cancels records the count.
* The history pairs the real rows, old ones too.

The access model is the production one. `access_model_v2` is on. The tests
write grants and admin rows to the legacy tables, and the migration-109
triggers mirror them. `bot_config` comes from the database, not from the
conftest stub.

These tests set the kill switch on, which cancels every running read that the
control database knows about. So they run only against a control database on
this host, a throwaway server, and skip everywhere else. Every row that they
write has a name that no real person or connection has.
"""
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from queryhub import (auto_approve, auto_approve_requests as aar, db,
                           kill_switch, targets)
from queryhub import config as cfg
from queryhub.config import ENV
from queryhub.crypto import encrypt
from queryhub.web import routes_admin as ra

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
    pytest.mark.skipif(ENV.bot_db_host not in ("127.0.0.1", "localhost", "::1"),
                       reason="sets the kill switch: runs only on a scratch control "
                              "database on this host"),
]

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "150_auto_approve_rw_requests.sql"
SUPER, CAPPED = "U0ITPKSUP1", "U0ITPKCAP1"          # admins: unscoped, capped at RW
DEV, READER = "U0ITPKDEV1", "U0ITPKRDR1"            # RW on both, RO on prod
PROD, STAGE, KILL = "it-pk-prod-main", "it-pk-stage-main", "it-pk-kill"
LOGINS = {"ro": "it_pk_ro", "rw": "it_pk_rw"}
NAMES = {SUPER: "IT PK Super", CAPPED: "IT PK Capped"}


@pytest.fixture(autouse=True)
def live_config(monkeypatch):
    """bot_config as production reads it: from the database, every call."""
    def get_setting(key, default=None):
        row = db.fetch_one("SELECT value FROM bot_config WHERE key = %s", (key,))
        if row is None:
            if default is None:
                raise KeyError(key)
            return default
        return row["value"]
    monkeypatch.setattr(cfg, "get_setting", get_setting)
    monkeypatch.setattr(cfg, "target_ssl_kwargs", lambda host=None: {"sslmode": "disable"})


def _set_config(key: str, value: str) -> None:
    db.execute("UPDATE bot_config SET value = %s WHERE key = %s", (value, key))


def _delete_audit_rows(people: list) -> None:
    """The runtime login may only add to audit_log (the split pass, see
    docs/OPERATIONS.md section 28), so a test that cleans up its own audit rows
    uses the superuser in QH_TARGET_ADMIN_DSN when that is set."""
    query = ("DELETE FROM audit_log WHERE request_id IN "
             "(SELECT id FROM requests WHERE requester_slack_id = ANY(%s))")
    dsn = os.environ.get("QH_TARGET_ADMIN_DSN")
    if not dsn:
        db.execute(query, (people,))
        return
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute(query, (people,))


def _wipe() -> None:
    """Delete every row of these people and connections, in both models. The
    mirror keeps a revoked copy of a legacy row, so this deletes the rows of
    the new tables after the legacy ones."""
    people = [SUPER, CAPPED, DEV, READER]
    t = [r["id"] for r in db.fetch_all(
        "SELECT id FROM target_servers WHERE alias = ANY(%s)", ([PROD, STAGE, KILL],))]
    db.execute("DELETE FROM auto_approve_grants WHERE slack_user_id = ANY(%s)", (people,))
    db.execute("DELETE FROM auto_approve_requests WHERE requester_slack_id = ANY(%s)", (people,))
    _delete_audit_rows(people)
    db.execute("DELETE FROM requests WHERE requester_slack_id = ANY(%s)", (people,))
    db.execute("DELETE FROM user_target_grants WHERE slack_user_id = ANY(%s)", (people,))
    db.execute("DELETE FROM admins WHERE slack_user_id = ANY(%s)", (people,))
    db.execute("DELETE FROM requesters WHERE slack_user_id = ANY(%s)", (people,))
    p = [r["principal_id"] for r in db.fetch_all(
        "SELECT principal_id FROM principal_identity "
        " WHERE provider = 'slack' AND external_id = ANY(%s)", (people,))]
    db.execute("DELETE FROM access_grant WHERE principal_id = ANY(%s) OR target_id = ANY(%s)",
               (p, t))
    db.execute("DELETE FROM role_assignment WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal_setting WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM team_member WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal_identity WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal WHERE id = ANY(%s)", (p,))
    db.execute("DELETE FROM target_servers WHERE id = ANY(%s)", (t,))


@pytest.fixture(scope="module")
def world():
    """Two connections and four people, in the production access model."""
    before = db.fetch_one("SELECT value FROM bot_config WHERE key = 'access_model_v2'")
    db.execute("UPDATE bot_config SET value = 'on' WHERE key = 'access_model_v2'")
    _wipe()
    ids = {}
    with db.transaction() as cur:
        for alias in (PROD, STAGE):
            cur.execute("INSERT INTO target_servers (alias, host, default_database, username, "
                        " password_encrypted, enabled) "
                        "VALUES (%s, 'scratch.example.internal', 'ledger', 'unused', "
                        " 'unused', TRUE) RETURNING id", (alias,))
            ids[alias] = cur.fetchone()["id"]
        cur.execute("INSERT INTO admins (slack_user_id, name, enabled) VALUES (%s, %s, TRUE)",
                    (SUPER, NAMES[SUPER]))
        cur.execute("INSERT INTO admins (slack_user_id, name, enabled, max_tier) "
                    "VALUES (%s, %s, TRUE, 'rw')", (CAPPED, NAMES[CAPPED]))
        for pid, name in ((DEV, "IT PK Dev"), (READER, "IT PK Reader")):
            cur.execute("INSERT INTO requesters (slack_user_id, name, enabled) "
                        "VALUES (%s, %s, TRUE)", (pid, name))
        grant = ("INSERT INTO user_target_grants (slack_user_id, target_server_id, mode) "
                 "VALUES (%s, %s, %s)")
        cur.execute(grant, (DEV, ids[PROD], "rw"))
        cur.execute(grant, (DEV, ids[STAGE], "rw"))
        cur.execute(grant, (READER, ids[PROD], "ro"))
    try:
        yield ids
    finally:
        _wipe()
        db.execute("UPDATE bot_config SET value = %s WHERE key = 'access_model_v2'",
                   (before["value"],))
        _set_config("auto_approve_rw_requests", "off")


# --- U3: the switch -------------------------------------------------------------------

def test_the_switch_is_seeded_off_and_a_rerun_keeps_a_set_value():
    row = db.fetch_one("SELECT value, description FROM bot_config "
                       " WHERE key = 'auto_approve_rw_requests'")
    assert row["value"] == "off" and "RW" in row["description"]
    _set_config("auto_approve_rw_requests", "on")
    try:
        with db.transaction() as cur:
            cur.execute(MIGRATION.read_text())
        assert db.fetch_one("SELECT value FROM bot_config "
                            " WHERE key = 'auto_approve_rw_requests'")["value"] == "on"
    finally:
        _set_config("auto_approve_rw_requests", "off")


def _ask(pid: str, target_id: int, tier: str = "rw"):
    return aar.submit_window(principal_id=pid, name="IT PK", target_id=target_id,
                             window_minutes=1440, reason="a day of backfill writes",
                             tier=tier)


def test_a_rw_request_waits_for_the_switch(world):
    with pytest.raises(aar.WindowRequestRefused) as e:
        _ask(DEV, world[PROD])
    assert (e.value.status, e.value.message) == (403, aar.RW_OFF_MESSAGE)
    assert db.fetch_one("SELECT count(*) AS n FROM auto_approve_requests "
                        " WHERE requester_slack_id = %s", (DEV,))["n"] == 0
    _set_config("auto_approve_rw_requests", "on")
    try:
        row, _t = _ask(DEV, world[PROD])
        assert row["max_tier"] == "rw" and row["status"] == "pending"
        with pytest.raises(aar.WindowRequestRefused) as e:
            _ask(READER, world[PROD])            # holds RO only
        assert e.value.status == 403 and "read-only" in e.value.message
    finally:
        _set_config("auto_approve_rw_requests", "off")
        db.execute("DELETE FROM auto_approve_requests WHERE requester_slack_id = %s", (DEV,))


def test_the_decisions_on_a_real_database(world):
    _set_config("auto_approve_rw_requests", "on")
    try:
        prod_req, _t = _ask(DEV, world[PROD])
        stage_req, _t = _ask(DEV, world[STAGE])

        # The admin screen lists the production RW window to the super-admin only.
        listed = lambda who: {r["id"] for r in ra.admin_window_requests(  # noqa: E731
            claims={"sub": who})["requests"]}
        assert prod_req["id"] in listed(SUPER) and prod_req["id"] not in listed(CAPPED)
        assert stage_req["id"] in listed(CAPPED)

        # Production: an admin who is not a super-admin cannot decide it.
        with pytest.raises(aar.WindowDecisionRefused) as e:
            aar.decide_window(prod_req["id"], approve=True, actor_id=CAPPED,
                              actor_name=NAMES[CAPPED])
        assert e.value.code == "super_admin"
        # The switch off: not even a super-admin approves it.
        _set_config("auto_approve_rw_requests", "off")
        with pytest.raises(aar.WindowDecisionRefused) as e:
            aar.decide_window(prod_req["id"], approve=True, actor_id=SUPER,
                              actor_name=NAMES[SUPER])
        assert e.value.code == "rw_off"
        assert db.fetch_one("SELECT count(*) AS n FROM auto_approve_grants "
                            " WHERE slack_user_id = %s", (DEV,))["n"] == 0
        _set_config("auto_approve_rw_requests", "on")

        out = aar.decide_window(prod_req["id"], approve=True, actor_id=SUPER,
                                actor_name=NAMES[SUPER])
        waiver = db.fetch_one("SELECT max_tier, expires_at, NOW() AS now "
                              "  FROM auto_approve_grants WHERE id = %s", (out["grant_id"],))
        assert waiver["max_tier"] == "rw"
        left = waiver["expires_at"] - waiver["now"]
        assert timedelta(minutes=1439) < left <= timedelta(minutes=1440)
        audit = db.fetch_one("SELECT details FROM audit_log "
                             " WHERE action = 'auto_approve_window_approved' "
                             "   AND details->>'request_id' = %s ORDER BY id DESC LIMIT 1",
                             (str(prod_req["id"]),))["details"]
        assert audit["max_tier"] == "rw"
        assert audit["expires_at"] == waiver["expires_at"].isoformat()
        req = db.fetch_one("SELECT status, granted_id FROM auto_approve_requests "
                           " WHERE id = %s", (prod_req["id"],))
        assert (req["status"], req["granted_id"]) == ("approved", out["grant_id"])

        # The waiver skips the review of a write now, and not after its end.
        target = targets.get(world[PROD])
        assert auto_approve.decision(DEV, "rw", target) is not None
        later = datetime.now(timezone.utc) + timedelta(days=2)
        assert auto_approve.decision(DEV, "rw", target, at_time=later) is None

        # Staging: the capped admin decides it.
        assert aar.decide_window(stage_req["id"], approve=True, actor_id=CAPPED,
                                 actor_name=NAMES[CAPPED])["status"] == "approved"
    finally:
        _set_config("auto_approve_rw_requests", "off")
        db.execute("DELETE FROM auto_approve_grants WHERE slack_user_id = %s", (DEV,))
        db.execute("DELETE FROM auto_approve_requests WHERE requester_slack_id = %s", (DEV,))


def test_rw_lost_before_the_decision_is_not_approved(world):
    _set_config("auto_approve_rw_requests", "on")
    try:
        req, _t = _ask(DEV, world[STAGE])
        db.execute("UPDATE user_target_grants SET mode = 'ro' "
                   " WHERE slack_user_id = %s AND target_server_id = %s",
                   (DEV, world[STAGE]))
        with pytest.raises(aar.WindowDecisionRefused) as e:
            aar.decide_window(req["id"], approve=True, actor_id=SUPER,
                              actor_name=NAMES[SUPER])
        assert e.value.code == "rw_not_held"
    finally:
        db.execute("UPDATE user_target_grants SET mode = 'rw' "
                   " WHERE slack_user_id = %s AND target_server_id = %s",
                   (DEV, world[STAGE]))
        _set_config("auto_approve_rw_requests", "off")
        db.execute("DELETE FROM auto_approve_requests WHERE requester_slack_id = %s", (DEV,))


# --- U4c: the kill switch stops the running read, not the write ----------------------

def _admin_conn():
    dsn = os.environ.get("QH_TARGET_ADMIN_DSN")
    if dsn:
        return psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    return psycopg.connect(host=ENV.bot_db_host, port=ENV.bot_db_port,
                           dbname=ENV.bot_db_name, user=ENV.bot_db_user,
                           password=ENV.bot_db_password, autocommit=True,
                           connect_timeout=10, **ENV.bot_db_ssl_kwargs())


def _drop_logins(admin) -> None:
    names = list(LOGINS.values())
    admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  " WHERE usename = ANY(%s)", (names,))
    deadline = time.monotonic() + 10
    while admin.execute("SELECT count(*) FROM pg_stat_activity WHERE usename = ANY(%s)",
                        (names,)).fetchone()[0]:
        assert time.monotonic() < deadline, "a test session did not end"
        time.sleep(0.1)
    for name in names:
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture(scope="module")
def server(world):
    admin = _admin_conn()
    row = admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
    if not (row and row[0]):
        admin.close()
        pytest.skip("cannot make logins: set QH_TARGET_ADMIN_DSN to a superuser conninfo")
    passwords = {tier: secrets.token_urlsafe(18) for tier in LOGINS}
    _drop_logins(admin)
    for tier, name in LOGINS.items():
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER PASSWORD {}").format(
            sql.Identifier(name), sql.Literal(passwords[tier])))
    admin.execute(sql.SQL("GRANT pg_read_all_stats TO {}").format(sql.Identifier(LOGINS["ro"])))
    where = {"host": admin.info.host, "port": admin.info.port, "dbname": admin.info.dbname}
    target_id = db.insert_returning(
        "INSERT INTO target_servers (alias, host, port, default_database, secrets_provider, "
        " username, password_encrypted, username_rw, password_rw_encrypted, enabled) "
        "VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, FALSE) RETURNING id",
        (KILL, where["host"], where["port"], where["dbname"],
         LOGINS["ro"], encrypt(passwords["ro"]), LOGINS["rw"], encrypt(passwords["rw"])))["id"]
    try:
        yield {"admin": admin, "where": where, "passwords": passwords, "target": target_id}
    finally:
        _drop_logins(admin)
        admin.close()


class _Running:
    """One `pg_sleep` as the login of a tier, in a session that the executor
    would open: autocommit, with an application_name that names the request."""

    def __init__(self, server: dict, tier: str, request_id: int):
        w = server["where"]
        self.admin, self.login = server["admin"], LOGINS[tier]
        self.conn = psycopg.connect(host=w["host"], port=w["port"], dbname=w["dbname"],
                                    user=self.login, password=server["passwords"][tier],
                                    autocommit=True, connect_timeout=10,
                                    application_name=f"queryhub req={request_id} by={DEV}")
        self.pid = self.conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        db.execute("UPDATE requests SET backend_pid = %s WHERE id = %s", (self.pid, request_id))
        self.result: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.active():
            assert time.monotonic() < deadline, "pg_sleep did not start"
            time.sleep(0.05)

    def _run(self) -> None:
        try:
            self.conn.execute("SELECT pg_sleep(60)")
            self.result = "finished"
        except psycopg.errors.QueryCanceled:
            self.result = "cancelled"
        except Exception as e:  # noqa: BLE001 - the test reads the name
            self.result = type(e).__name__

    def active(self) -> bool:
        row = self.admin.execute("SELECT state FROM pg_stat_activity WHERE pid = %s",
                                 (self.pid,)).fetchone()
        return row == ("active",)

    def close(self) -> None:
        self.admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                           " WHERE pid = %s AND usename = %s", (self.pid, self.login))
        self.thread.join(timeout=10)
        self.conn.close()


def _running_request(server: dict, tier: str) -> int:
    return db.insert_returning(
        "INSERT INTO requests (requester_slack_id, target_server_id, database_name, query, "
        " status, executed_tier, executed_at) "
        "VALUES (%s, %s, %s, 'SELECT pg_sleep(60)', 'executing', %s, NOW()) RETURNING id",
        (DEV, server["target"], server["where"]["dbname"], tier))["id"]


def _last_row(action: str = "kill_switch_set") -> dict:
    return db.fetch_one("SELECT id, actor_slack_id, details FROM audit_log "
                        " WHERE action = %s ORDER BY id DESC LIMIT 1", (action,))


def test_on_stops_the_running_read_and_leaves_the_write(server):
    read_id = _running_request(server, "ro")
    write_id = _running_request(server, "rw")
    read, write = _Running(server, "ro", read_id), _Running(server, "rw", write_id)
    claims = {"sub": SUPER, "name": NAMES[SUPER]}
    try:
        out = ra.set_kill(ra.KillIn(enabled=True, message="Paused for a migration."),
                          claims=claims)
        assert out["enabled"] is True and out["cancelledCount"] == 1
        read.thread.join(timeout=15)
        assert read.result == "cancelled"
        assert write.active() and write.result is None
        st = {r["id"]: r for r in db.fetch_all(
            "SELECT id, status::text AS st, error_message FROM requests "
            " WHERE id = ANY(%s)", ([read_id, write_id],))}
        assert st[read_id]["st"] == "failed" and NAMES[SUPER] in st[read_id]["error_message"]
        assert st[write_id]["st"] == "executing"
        pause, count = _last_row(), _last_row("kill_switch_cancelled")
        assert pause["actor_slack_id"] == SUPER == count["actor_slack_id"]
        assert pause["details"] == {"enabled": True, "message": "Paused for a migration."}
        assert count["details"] == {"cancelledCount": 1}
        assert db.fetch_one("SELECT value FROM bot_config "
                            " WHERE key = 'kill_switch'")["value"] == "on"
        # The order on the record: the pause, then the cancel of the read, then
        # the count. The switch was on before the first cancel.
        cancels = db.fetch_all("SELECT id, action FROM audit_log WHERE request_id = %s "
                               " ORDER BY id", (read_id,))
        assert [r["action"] for r in cancels] == ["cancel_requested", "cancelled_running"]
        assert pause["id"] < cancels[0]["id"] < cancels[-1]["id"] < count["id"]
        assert db.fetch_all("SELECT 1 FROM audit_log WHERE request_id = %s", (write_id,)) == []

        out = ra.set_kill(ra.KillIn(enabled=False), claims=claims)
        assert out["cancelledCount"] == 0 and write.active()
        assert _last_row()["details"] == {"enabled": False}
        assert _last_row("kill_switch_cancelled")["id"] == count["id"]   # off counts nothing
    finally:
        read.close()
        write.close()
        _set_config("kill_switch", "off")
        db.execute("UPDATE requests SET status = 'failed', completed_at = NOW(), "
                   " error_message = 'integration test cleanup' "
                   " WHERE id = ANY(%s) AND status = 'executing'", ([read_id, write_id],))


# --- U4b: the history pairs the real rows --------------------------------------------

def test_the_history_pairs_the_real_rows(server):
    from queryhub import audit
    from queryhub.slack_app import subcommands
    claims = {"sub": SUPER, "name": NAMES[SUPER]}
    try:
        # A pause as the route wrote it before the message and the count.
        with db.transaction() as cur:
            audit.log_in(cur, None, CAPPED, NAMES[CAPPED], "kill_switch_set", {"enabled": True})
        with db.transaction() as cur:
            audit.log_in(cur, None, SUPER, NAMES[SUPER], "kill_switch_set", {"enabled": False})
        ra.set_kill(ra.KillIn(enabled=True, message="Disk alarm."), claims=claims)
        ra.set_kill(ra.KillIn(enabled=False), claims=claims)
        subcommands._handle_kill(SUPER, "on", None, lambda *a, **k: None, {})

        newest = ra.kill_history(claims={"sub": CAPPED})[:3]
        open_, closed, old = newest
        assert (open_["by"], open_["endedAt"], open_["endedBy"]) == (NAMES[SUPER], None, None)
        assert (open_["message"], open_["cancelledCount"]) == ("Disk alarm.", 0)
        assert (closed["message"], closed["cancelledCount"]) == ("Disk alarm.", 0)
        assert closed["endedBy"] == NAMES[SUPER] and closed["endedAt"] > closed["startedAt"]
        assert (old["by"], old["message"], old["cancelledCount"]) == (NAMES[CAPPED], None, None)
        assert old["endedBy"] == NAMES[SUPER]
        assert open_["startedAt"] > closed["startedAt"] > old["startedAt"]
        assert kill_switch.history()[:3] == newest
    finally:
        ra.set_kill(ra.KillIn(enabled=False), claims=claims)
        _set_config("kill_switch", "off")
