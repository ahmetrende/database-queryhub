"""Read-only session and lock viewer (DBeaver item P2.6), server side.

Nothing here reaches a database. The connections and cursors are fakes that
answer each viewer statement with fixed rows.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from queryhub import pii, session_viewer as sv, targets
from queryhub.web import admin as web_admin
from queryhub.web import routes_admin as ra

SUPER = {"sub": "U000EXAMPLE", "name": "Super Admin"}
DBA = {"sub": "U000SCOPED1", "name": "Scoped Dba"}
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _target(engine: str = "postgres") -> targets.TargetServer:
    return targets.TargetServer(
        id=42, alias="prod-beta", host="db.example.internal", port=5432,
        default_database="ledger", username="reader", enabled=True, notes=None,
        engine=engine, engine_config={}, ai_allowed=True, mcp_allowed=True)


class FakeCursor:
    def __init__(self, answers: dict[str, tuple[list[str], list[tuple]]]):
        self.answers = answers
        self.sql: list[str] = []
        self.description: list[tuple] | None = None
        self._rows: list[tuple] = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        cols, rows = self.answers[sql]
        self.description = [(c,) for c in cols]
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur: FakeCursor):
        self.cur = cur
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cur

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


PG_SESSION_COLS = ["pid", "usename", "application_name", "client_addr", "state",
                   "backend_type", "backend_start", "xact_start", "query_start",
                   "state_change", "wait_event_type", "wait_event", "blocked_by",
                   "datname", "query"]
PG_LOCK_COLS = ["pid", "locktype", "mode", "granted", "relation", "xid"]


def _pg_session(pid, query="SELECT 1", blocked_by=(), user="app", state="active"):
    return (pid, user, "psql", "192.0.2.5", state, "client backend", T0, T0, T0,
            T0, None, None, list(blocked_by), "ledger", query)


def _pg_answers(sessions, locks=()):
    return {sv.PG_SESSIONS_SQL: (PG_SESSION_COLS, list(sessions)),
            sv.PG_LOCKS_SQL: (PG_LOCK_COLS, list(locks))}


MS_SESSION_COLS = ["session_id", "login_name", "program_name", "host_name",
                   "status", "login_time", "command", "wait_type", "wait_time",
                   "blocking_session_id", "start_time", "last_request_start_time",
                   "database_name", "query_text"]
MS_LOCK_COLS = ["request_session_id", "resource_type", "request_mode",
                "request_status", "relation"]


def _ms_answers(sessions, locks=(), has_state=1):
    return {sv.MSSQL_PERMISSION_SQL: (["has_state"], [(has_state,)]),
            sv.MSSQL_SESSIONS_SQL: (MS_SESSION_COLS, list(sessions)),
            sv.MSSQL_LOCKS_SQL: (MS_LOCK_COLS, list(locks))}


@pytest.fixture
def masking_off(monkeypatch):
    monkeypatch.setattr(sv.pii, "is_enabled", lambda: False)


@pytest.fixture
def wired(monkeypatch):
    """fetch() with fake credentials and a fake connection per engine."""
    st: dict = {"cur": None, "conn": None}
    monkeypatch.setattr(sv.targets, "get_credentials",
                        lambda tid, mode: ("reader_ro", "pw") if mode == "ro"
                        else pytest.fail("the viewer must use the RO login"))

    def _conn(target, user, password, database):
        st["login"] = (user, database)
        st["conn"] = FakeConn(st["cur"])
        return st["conn"]

    monkeypatch.setattr(sv, "_connect_pg", _conn)
    monkeypatch.setattr(sv, "_connect_mssql", _conn)
    return st


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_postgres_rows_are_normalized_with_blocking(wired, masking_off):
    wired["cur"] = FakeCursor(_pg_answers(
        [_pg_session(101), _pg_session(102, blocked_by=[101]),
         _pg_session(103, blocked_by=[101, 102])],
        [(102, "relation", "AccessExclusiveLock", False, "public.orders", None)]))
    out = sv.fetch(_target(), "ledger")

    assert out["engine"] == "postgres" and out["database"] == "ledger"
    assert out["fetchedAt"].endswith("+00:00")
    assert wired["login"] == ("reader_ro", "ledger")
    assert wired["conn"].rolled_back
    s = {r["id"]: r for r in out["sessions"]}
    assert s[101]["blockedBy"] == [] and s[101]["blocking"] == [102, 103]
    assert s[102]["blockedBy"] == [101] and s[102]["blocking"] == [103]
    assert s[103]["blocking"] == []
    assert s[101]["user"] == "app" and s[101]["app"] == "psql"
    assert s[101]["started"] == T0.isoformat()
    assert s[101]["query"] == "SELECT 1" and s[101]["queryHidden"] is False
    assert out["locks"] == [{"id": 102, "type": "relation",
                             "mode": "AccessExclusiveLock", "granted": False,
                             "relation": "public.orders"}]
    assert out["notes"] == []


def test_sql_server_rows_are_normalized_with_blocking(wired, masking_off):
    wired["cur"] = FakeCursor(_ms_answers(
        [(51, "app", "SSMS", "WS1", "running", T0, "UPDATE", None, None, 0, T0,
          T0, "ledger", "UPDATE t SET a = 1"),
         (52, "app", "SSMS", "WS2", "suspended", T0, "SELECT", "LCK_M_S", 1500,
          51, T0, T0, "ledger", "SELECT * FROM t"),
         (53, "app", None, "WS3", "sleeping", T0, None, None, None, -2, None,
          T0, "ledger", None)],
        [(52, "OBJECT", "IS", "WAIT", "dbo.t"),
         (51, "KEY", "X", "GRANT", None)]))
    out = sv.fetch(_target("mssql"), "ledger")

    assert out["engine"] == "mssql"
    assert wired["conn"].closed and wired["conn"].rolled_back
    s = {r["id"]: r for r in out["sessions"]}
    assert s[52]["blockedBy"] == [51] and s[51]["blocking"] == [52]
    assert s[53]["blockedBy"] == []          # -2 is not a session
    assert s[52]["wait"] == "LCK_M_S (1500 ms)"
    assert s[52]["state"] == "suspended (SELECT)"
    assert s[53]["app"] is None and s[53]["query"] is None
    assert s[53]["queryHidden"] is False
    assert out["locks"][0] == {"id": 52, "type": "OBJECT", "mode": "IS",
                               "granted": False, "relation": "dbo.t"}
    assert out["locks"][1]["granted"] is True
    assert out["notes"] == []


def test_sql_server_without_view_server_state_says_so(wired, masking_off):
    wired["cur"] = FakeCursor(_ms_answers([], has_state=0))
    out = sv.fetch(_target("mssql"), "ledger")
    assert out["notes"] == [sv.NOTE_MSSQL_OWN_ONLY]
    assert out["locks"] == []
    # The lock DMV needs the same permission, so it is not read at all.
    assert sv.MSSQL_LOCKS_SQL not in wired["cur"].sql


def test_insufficient_privilege_hides_the_text_and_adds_one_note(wired, masking_off):
    wired["cur"] = FakeCursor(_pg_answers(
        [_pg_session(201, "<insufficient privilege>"),
         _pg_session(202, "<insufficient privilege>"),
         _pg_session(203, "SELECT now()")]))
    out = sv.fetch(_target(), "ledger")
    s = {r["id"]: r for r in out["sessions"]}
    assert s[201]["query"] is None and s[201]["queryHidden"] is True
    assert s[203]["query"] == "SELECT now()" and s[203]["queryHidden"] is False
    assert out["notes"] == [sv.NOTE_PG_NO_STATS]


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

def _email_detectors():
    pack = next(iter(pii.REGION_PACKS.values()))
    found = [d for d in pack if d.name == "email"]
    assert found, "the detector pack has no email detector"
    return found


def test_an_email_in_the_query_text_is_masked(wired, monkeypatch):
    monkeypatch.setattr(sv.pii, "is_enabled", lambda: True)
    monkeypatch.setattr(pii, "active_detectors", _email_detectors)
    wired["cur"] = FakeCursor(_pg_answers(
        [_pg_session(301, "SELECT * FROM users WHERE email = 'alice@example.com'")]))
    out = sv.fetch(_target(), "ledger")
    q = out["sessions"][0]["query"]
    assert q is not None and "alice@example.com" not in q
    assert q.startswith("SELECT * FROM users WHERE email = ")
    assert out["sessions"][0]["queryHidden"] is False


def test_a_masking_error_hides_the_text(wired, monkeypatch):
    monkeypatch.setattr(sv.pii, "is_enabled", lambda: True)

    def _boom(value, found, _depth=0):
        raise RuntimeError("detector failed")

    monkeypatch.setattr(sv.pii, "mask_value", _boom)
    wired["cur"] = FakeCursor(_pg_answers(
        [_pg_session(302, "SELECT 'alice@example.com'")]))
    out = sv.fetch(_target(), "ledger")
    assert out["sessions"][0]["query"] is None
    assert out["sessions"][0]["queryHidden"] is True


def test_a_failing_masking_switch_hides_the_text(monkeypatch):
    def _boom():
        raise KeyError("bot_config unreachable")

    monkeypatch.setattr(sv.pii, "is_enabled", _boom)
    assert sv._mask_query("SELECT 'alice@example.com'") == (None, True)


def test_an_unsupported_engine_is_refused(wired):
    with pytest.raises(ValueError, match="not available for this engine"):
        sv.fetch(_target("clickhouse"), "default")
    assert wired["conn"] is None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

class FakeTxn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


@pytest.fixture
def route(monkeypatch):
    st: dict = {"super": True, "audit": [], "known": True, "fetch": [],
                "error": None}
    monkeypatch.setattr(web_admin.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(web_admin.admins, "is_super_admin", lambda uid: st["super"])
    monkeypatch.setattr(ra.admins, "is_super_admin", lambda uid: st["super"])
    monkeypatch.setattr(ra.targets, "by_alias",
                        lambda alias: (type("T", (), {"id": 42})()
                                       if st["known"] and alias == "prod-beta"
                                       else None))
    monkeypatch.setattr(ra.targets, "admin_row",
                        lambda tid: {"id": 42, "alias": "prod-beta"})
    monkeypatch.setattr(ra.targets, "get", lambda tid: _target())

    def _fetch(target, database):
        st["fetch"].append(database)
        if st["error"] is not None:
            raise st["error"]
        return {"engine": "postgres", "database": database, "fetchedAt": "x",
                "sessions": [{"id": 1}, {"id": 2}], "locks": [{"id": 1}],
                "notes": []}

    monkeypatch.setattr(ra.session_viewer, "fetch", _fetch)
    monkeypatch.setattr(ra.db, "transaction", lambda: FakeTxn(object()))
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((uid, action, details)))
    return st


def test_the_route_is_a_get():
    methods = {m for r in ra.router.routes
               if getattr(r, "path", "") == "/api/admin/connections/{conn}/sessions"
               for m in getattr(r, "methods", set())}
    assert "GET" in methods


def test_a_super_admin_reads_sessions_and_it_is_audited(route):
    out = ra.admin_connection_sessions("prod-beta", None, claims=SUPER)
    assert out["database"] == "ledger"        # the default database
    assert route["audit"] == [(SUPER["sub"], "sessions_viewed",
                               {"target_id": 42, "database": "ledger",
                                "sessions": 2, "locks": 1})]


def test_the_database_parameter_is_passed_on(route):
    ra.admin_connection_sessions("prod-beta", "billing", claims=SUPER)
    assert route["fetch"] == ["billing"]


def test_a_non_super_admin_gets_403(route):
    route["super"] = False
    with pytest.raises(HTTPException) as e:
        ra.admin_connection_sessions("prod-beta", None, claims=DBA)
    assert e.value.status_code == 403
    assert route["fetch"] == [] and route["audit"] == []


def test_an_unknown_connection_is_404(route):
    route["known"] = False
    with pytest.raises(HTTPException) as e:
        ra.admin_connection_sessions("nope", None, claims=SUPER)
    assert e.value.status_code == 404
    assert route["audit"] == []


def test_an_unsupported_engine_is_400(route):
    route["error"] = ValueError(sv.UNSUPPORTED)
    with pytest.raises(HTTPException) as e:
        ra.admin_connection_sessions("prod-beta", None, claims=SUPER)
    assert e.value.status_code == 400
    assert route["audit"] == []


def test_an_unreachable_target_is_502(route, masking_off):
    route["error"] = OSError("connection refused")
    with pytest.raises(HTTPException) as e:
        ra.admin_connection_sessions("prod-beta", None, claims=SUPER)
    assert e.value.status_code == 502
    assert e.value.detail["code"] == "upstream"
    assert route["audit"] == []
