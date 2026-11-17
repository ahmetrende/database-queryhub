"""The audit comment in front of each statement that QueryHub sends.

bot_config `sql_audit_comment` turns it on. Some audit logs keep the statement
text and lose `application_name`, so the comment carries the origin, the
request id and the requester.
"""
import pytest

from queryhub import config as cfg
from queryhub import executor as ex
from queryhub import pii_lineage, pre_flight, profile_sync
from queryhub import sql_audit_comment as sac
from queryhub import targets

EMAILS = {"U0AB12CD34": "jordan.ray@example.com"}


@pytest.fixture
def switch(monkeypatch):
    """bot_config with the switch value that the test sets, and a fixed email
    directory."""
    state = {"value": "off"}
    defaults = cfg.get_setting          # the conftest stub

    def get_setting(key, default=None):
        if key == sac.SETTING:
            return state["value"]
        return defaults(key, default)

    monkeypatch.setattr(cfg, "get_setting", get_setting)
    monkeypatch.setattr(profile_sync, "lookup_email", EMAILS.get)
    return state


# --- the comment -----------------------------------------------------------

def test_the_comment_names_the_origin_the_request_and_the_email(switch):
    assert sac.comment(origin="web", request_id=5369, principal="U0AB12CD34") == (
        "/* QueryHub (web) req_id=5369 by=jordan.ray@example.com */")


def test_without_an_email_the_comment_names_the_principal_id(switch):
    assert sac.comment(origin=None, request_id=7, principal="U0XYZ") == (
        "/* QueryHub (slack) req_id=7 by=U0XYZ */")


def test_a_local_user_id_keeps_only_safe_characters(switch):
    text = sac.comment(origin="web", request_id=8, principal="local:jordan ray")
    assert text == "/* QueryHub (web) req_id=8 by=local_jordan_ray */"


def test_a_value_cannot_close_the_comment(switch, monkeypatch):
    monkeypatch.setattr(profile_sync, "lookup_email",
                        lambda _pid: "x*/ DROP TABLE t; /*@example.com")
    text = sac.comment(origin="we*/b", request_id=1, principal="U1")
    assert text.count("*/") == 1 and text.endswith(" */")
    assert "DROP TABLE" not in text and " t;" not in text


def test_before_the_request_exists_the_comment_says_req_id_none(switch):
    assert sac.comment(origin="mcp", request_id=None, principal="U0AB12CD34") == (
        "/* QueryHub (mcp) req_id=none by=jordan.ray@example.com */")


def test_an_email_lookup_error_falls_back_to_the_principal_id(switch, monkeypatch):
    def boom(_pid):
        raise RuntimeError("metadata database is down")
    monkeypatch.setattr(profile_sync, "lookup_email", boom)
    assert sac.comment(origin="slack", request_id=2, principal="U0R") == (
        "/* QueryHub (slack) req_id=2 by=U0R */")


def test_the_switch_is_off_by_default_and_then_no_text_changes(switch):
    assert sac.enabled() is False
    assert sac.for_request({"id": 1, "requester_slack_id": "U0AB12CD34"}) == ""


@pytest.mark.parametrize("value", ["on", "true", "1"])
def test_the_switch_on_gives_the_comment_and_one_space(switch, value):
    switch["value"] = value
    assert sac.for_request({"id": 41, "origin": "idp",
                            "requester_slack_id": "U0AB12CD34"}) == (
        "/* QueryHub (idp) req_id=41 by=jordan.ray@example.com */ ")


# --- SQL Server modules ----------------------------------------------------

@pytest.mark.parametrize("sql", [
    "CREATE PROCEDURE dbo.p AS SELECT 1",
    "create proc dbo.p as select 1",
    "CREATE OR ALTER PROCEDURE dbo.p AS SELECT 1",
    "ALTER FUNCTION dbo.f() RETURNS int AS BEGIN RETURN 1 END",
    "CREATE VIEW dbo.v AS SELECT 1 AS a",
    "CREATE  OR\n ALTER   TRIGGER dbo.t ON dbo.x AFTER INSERT AS SELECT 1",
    "-- note\n/* a /* nested */ block */\n  CREATE VIEW dbo.v AS SELECT 1 AS a",
])
def test_a_sql_server_module_statement_gets_the_comment_in_its_own_batch(sql):
    assert sac.needs_own_batch(sql, "mssql") is True


@pytest.mark.parametrize("sql, engine", [
    ("CREATE PROCEDURE dbo.p AS SELECT 1", "postgres"),
    ("CREATE FUNCTION f() RETURNS int LANGUAGE sql AS 'SELECT 1'", None),
    ("CREATE TABLE dbo.t (id int)", "mssql"),
    ("SELECT 'CREATE VIEW' AS a", "mssql"),
    ("UPDATE dbo.t SET a = 1 WHERE id = 1", "mssql"),
    ("CREATE PROCEDURES_LOG", "mssql"),
    ("/* never closed CREATE VIEW v AS SELECT 1", "mssql"),
])
def test_other_statements_get_the_comment_in_their_own_text(sql, engine):
    assert sac.needs_own_batch(sql, engine) is False


# --- the executor ----------------------------------------------------------

class _Cur:
    """A cursor without a result set that records what it receives."""
    description = None
    rowcount = 1

    def __init__(self):
        self.sent = []

    def execute(self, sql, prepare=False):
        self.sent.append(sql)


def _stmt(sql, leading):
    return type("S", (), {"rewritten": sql, "leading": leading, "raw": sql})()


TAG = "/* QueryHub (web) req_id=9 by=jordan.ray@example.com */ "


def test_the_executor_sends_the_statement_with_the_comment_in_front():
    cur = _Cur()
    ex._execute_main_statement(cur, _stmt("UPDATE t SET a = 1", "UPDATE"),
                               1, 9, False, 100, 1000, audit_tag=TAG)
    assert cur.sent == [TAG + "UPDATE t SET a = 1"]


def test_without_a_tag_the_executor_sends_the_statement_unchanged():
    cur = _Cur()
    ex._execute_main_statement(cur, _stmt("UPDATE t SET a = 1", "UPDATE"),
                               1, 9, False, 100, 1000)
    assert cur.sent == ["UPDATE t SET a = 1"]


def test_a_streamed_read_gets_the_comment_too():
    class _StreamCur(_Cur):
        rowcount = -1

        def stream(self, sql):
            self.sent.append(sql)
            return iter(())

    cur = _StreamCur()
    ex._execute_main_statement(cur, _stmt("SELECT 1", "SELECT"), 1, 9, False,
                               100, 1000, force_extended=True, audit_tag=TAG)
    assert cur.sent == [TAG + "SELECT 1"]


def test_a_sql_server_procedure_gets_the_comment_as_its_own_batch(monkeypatch):
    monkeypatch.setattr(ex.stmt_guard, "check", lambda *a, **k: None)
    cur = _Cur()
    sql = "CREATE PROCEDURE dbo.p AS SELECT 1"
    ex._execute_main_statement(cur, _stmt(sql, "CREATE"), 1, 9, False, 100,
                               1000, engine="mssql", audit_tag=TAG)
    assert cur.sent == [TAG.rstrip(), sql]


def test_a_sql_server_query_gets_the_comment_in_its_text(monkeypatch):
    monkeypatch.setattr(ex.stmt_guard, "check", lambda *a, **k: None)
    cur = _Cur()
    ex._execute_main_statement(cur, _stmt("DELETE FROM dbo.t WHERE id = 1", "DELETE"),
                               1, 9, False, 100, 1000, engine="mssql",
                               audit_tag=TAG)
    assert cur.sent == [TAG + "DELETE FROM dbo.t WHERE id = 1"]


def test_the_lineage_explain_gets_the_comment():
    sent = []

    class _LineageCur:
        def execute(self, sql):
            sent.append(sql)

        def fetchone(self):
            return [[{"Plan": {"Output": ["t.a"]}}]]

    pii_lineage.source_columns(_LineageCur(), "SELECT a FROM t", tag=TAG)
    assert TAG + "EXPLAIN (VERBOSE, FORMAT JSON, COSTS OFF) SELECT a FROM t" in sent


# --- the pre-flight EXPLAIN ------------------------------------------------

class _PfCursor:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, **kwargs):
        self.sink.append(sql)

    def fetchone(self):
        return [[{"Plan": {"Node Type": "ModifyTable", "Plan Rows": 0,
                           "Plans": [{"Node Type": "Seq Scan", "Plan Rows": 3}]}}]]


class _PfConn:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _PfCursor(self.sink)


@pytest.fixture
def pf_target(monkeypatch):
    sent = []
    monkeypatch.setattr(pre_flight.psycopg, "connect", lambda **kw: _PfConn(sent))
    monkeypatch.setattr(pre_flight.targets, "get", lambda tid: targets.TargetServer(
        id=tid, alias="prod-ledger", host="primary.example.test", port=5432,
        default_database="ledger", username="reader", enabled=True, notes=None,
        engine="postgres"))
    monkeypatch.setattr(pre_flight.targets, "get_credentials",
                        lambda tid, mode: ("login", "pw"))
    monkeypatch.setattr(pre_flight.cfg, "target_ssl_kwargs", lambda host: {})
    monkeypatch.setattr("queryhub.pii.is_enabled", lambda: False)
    return sent


def _explains(sent):
    return [s for s in sent if "EXPLAIN" in s]


def test_the_pre_flight_explain_gets_the_comment(pf_target):
    pre_flight.explain(1, "ledger", "ro", "SELECT a FROM t", tag=TAG)
    assert _explains(pf_target) and all(s.startswith(TAG + "EXPLAIN (")
                                        for s in _explains(pf_target))


def test_the_write_estimate_explain_gets_the_comment(pf_target):
    pre_flight.explain_write_estimate(1, "ledger", "rw",
                                      "UPDATE t SET a = 1 WHERE id = 2", tag=TAG)
    assert _explains(pf_target) == [
        TAG + "EXPLAIN (FORMAT JSON, ANALYZE OFF, COSTS ON, BUFFERS OFF) "
        "UPDATE t SET a = 1 WHERE id = 2"]


def test_each_script_statement_explain_gets_the_comment(pf_target):
    from queryhub import query_safety
    stmts = query_safety.analyze("SELECT a FROM t; SELECT b FROM u",
                                 "postgres").statements
    ok, _err, _hint = pre_flight.validate_statements(1, "ledger", "ro", stmts,
                                                     tag=TAG)
    assert ok
    assert len(_explains(pf_target)) == 2
    assert all(s.startswith(TAG + "EXPLAIN (") for s in _explains(pf_target))
