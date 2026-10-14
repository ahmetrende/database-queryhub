"""Submit-time validation of the WHOLE request: every statement planned, none run.

Before this, `explain()` planned one read-only statement and skipped the rest, and
`explain_write_estimate()` planned one write but threw its errors away. A typo in
an UPDATE, or in the third SELECT of a script, therefore reached an approver, was
approved, and the requester met the error only when it ran.

What this file pins:

- every statement of a script gets its own EXPLAIN, in order, on ONE connection;
- the session is read-only and nothing but EXPLAIN (ANALYZE OFF) is ever sent;
- a request-level mistake (syntax, unknown column or table, bad type) is refused
  and names the statement; an environment problem fails OPEN;
- SET statements are not planned, and checking stops at the first DDL statement;
- `core_submit` calls it for every RW request and every multi-statement script,
  and the runtime kill switch restores the old behaviour.
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg import errors as pgerr

from queryhub import core_submit as cs
from queryhub import lifecycle
from queryhub import pre_flight as pf
from queryhub import query_safety, targets


# --- a fake target connection ----------------------------------------------------

def _update_plan(rows):
    """What EXPLAIN (FORMAT JSON) returns for an UPDATE that touches `rows` rows."""
    return [{"Plan": {"Node Type": "ModifyTable", "Plan Rows": 0,
                      "Plans": [{"Node Type": "Seq Scan", "Plan Rows": rows}]}}]


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, **kwargs):
        self.conn.executed.append(sql)
        if sql.startswith("EXPLAIN"):
            self.conn.explained.append(sql)
            self.conn.explain_kwargs.append(kwargs)
            if self.conn.on_explain is not None:
                self.conn.on_explain(len(self.conn.explained), sql)
            self._row = [self.conn.plan]

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.executed: list[str] = []
        self.explained: list[str] = []
        self.explain_kwargs: list[dict] = []
        self.on_explain = None
        self.plan = [{"Plan": {"Node Type": "Seq Scan", "Plan Rows": 1}}]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cursor(self)


@pytest.fixture
def target_db(monkeypatch):
    """Patch everything that would reach a database. `state` holds the fake
    connection of the last psycopg.connect() and a count of connects."""
    state = {"connects": 0, "conn": None, "engine": "postgres", "connect_error": None,
             "on_explain": None, "plan": None}

    def fake_connect(**kwargs):
        state["connects"] += 1
        if state["connect_error"] is not None:
            raise state["connect_error"]
        conn = _Connection(kwargs)
        conn.on_explain = state["on_explain"]
        if state["plan"] is not None:
            conn.plan = state["plan"]
        state["conn"] = conn
        return conn

    monkeypatch.setattr(pf.psycopg, "connect", fake_connect)
    monkeypatch.setattr(pf.targets, "get", lambda tid: targets.TargetServer(
        id=tid, alias="prod-ledger", host="primary.example.test", port=5432,
        default_database="ledger", username="reader", enabled=True, notes=None,
        engine=state["engine"]))
    monkeypatch.setattr(pf.targets, "get_credentials", lambda tid, mode: ("login", "pw"))
    monkeypatch.setattr(pf.cfg, "target_ssl_kwargs", lambda host: {})
    # errors.scrub() asks the PII layer, which reads bot_config. Not under test.
    monkeypatch.setattr("queryhub.pii.is_enabled", lambda: False)
    return state


def _stmts(sql, engine="postgres", unrestricted=False):
    return query_safety.analyze(sql, engine, unrestricted=unrestricted).statements


def _fail_on(n, exc):
    """on_explain hook: raise `exc` on the n-th EXPLAIN."""
    def hook(count, _sql):
        if count == n:
            raise exc
    return hook


# --- every statement, one connection ----------------------------------------------

def test_each_statement_of_a_script_is_planned_in_order_on_one_connection(target_db):
    ok, err, hint = pf.validate_statements(
        1, "ledger", "ro", _stmts("SELECT a FROM t; SELECT b FROM u; SELECT c FROM v"))
    assert (ok, err, hint) == (True, None, None)
    assert target_db["connects"] == 1
    explained = target_db["conn"].explained
    assert [s.split(") ", 1)[1] for s in explained] == [
        "SELECT a FROM t", "SELECT b FROM u", "SELECT c FROM v"]
    assert all(s.startswith("EXPLAIN (FORMAT JSON, ANALYZE OFF") for s in explained)


def test_nothing_but_explain_is_ever_sent_and_the_session_is_read_only(target_db):
    pf.validate_statements(
        1, "ledger", "rw", _stmts("UPDATE t SET a = 1 WHERE id = 1; DELETE FROM u WHERE x = 2"))
    sent = target_db["conn"].executed
    assert sent[0] == "SET TRANSACTION READ ONLY"
    assert sent[1] == "SET LOCAL search_path = pg_catalog, public"
    for sql in sent[2:]:
        assert sql.startswith(("EXPLAIN (", "SET LOCAL statement_timeout = ")), sql
    assert target_db["conn"].kwargs["dbname"] == "ledger"
    assert "lock_timeout=1000" in target_db["conn"].kwargs["options"]


def test_each_statement_gets_a_bounded_statement_timeout(target_db):
    pf.validate_statements(1, "ledger", "ro", _stmts("SELECT 1; SELECT 2"))
    timeouts = [int(s.rsplit("= ", 1)[1]) for s in target_db["conn"].executed
                if s.startswith("SET LOCAL statement_timeout")]
    assert len(timeouts) == 2
    assert all(150 <= t <= 2000 for t in timeouts)


# --- a mistake is refused and named -----------------------------------------------

def test_a_typo_in_the_second_statement_names_the_statement(target_db):
    target_db["on_explain"] = _fail_on(
        2, pgerr.UndefinedColumn('column "stauts" does not exist'))
    ok, err, hint = pf.validate_statements(
        1, "ledger", "ro", _stmts("SELECT a FROM t; SELECT stauts FROM u; SELECT c FROM v"))
    assert ok is False and hint is None
    assert err.startswith("Statement 2 of 3: ")
    assert 'Column "stauts" does not exist' in err


def test_a_single_statement_error_has_no_position_prefix(target_db):
    target_db["on_explain"] = _fail_on(
        1, pgerr.UndefinedColumn('column "stauts" does not exist'))
    ok, err, _ = pf.validate_statements(
        1, "ledger", "rw", _stmts("UPDATE orders SET stauts = 'x' WHERE id = 5"))
    assert ok is False
    assert err == 'Column "stauts" does not exist'


@pytest.mark.parametrize("exc", [
    pgerr.SyntaxError('syntax error at or near "FORM"'),
    pgerr.UndefinedTable('relation "ordrs" does not exist'),
    pgerr.UndefinedFunction("function foo(integer) does not exist"),
    pgerr.InvalidTextRepresentation('invalid input syntax for type integer: "abc"'),
    pgerr.FeatureNotSupported("cannot do that"),
])
def test_request_level_errors_are_refused(target_db, exc):
    target_db["on_explain"] = _fail_on(1, exc)
    ok, err, _ = pf.validate_statements(
        1, "ledger", "rw", _stmts("UPDATE t SET a = 1 WHERE id = 1"))
    assert ok is False and err


# --- the environment never blocks a request ---------------------------------------

@pytest.mark.parametrize("exc", [
    pgerr.QueryCanceled("canceling statement due to statement timeout"),
    pgerr.LockNotAvailable("could not obtain lock"),
    pgerr.DiskFull("disk full"),
    pgerr.ReadOnlySqlTransaction("cannot execute in a read-only transaction"),
    pgerr.InFailedSqlTransaction("current transaction is aborted"),
    # The run may use a team role with other privileges than the login planned with.
    pgerr.InsufficientPrivilege("permission denied for table orders"),
])
def test_environment_problems_fail_open(target_db, exc):
    target_db["on_explain"] = _fail_on(1, exc)
    assert pf.validate_statements(
        1, "ledger", "rw", _stmts("UPDATE t SET a = 1 WHERE id = 1")) == (True, None, None)


def test_an_unreachable_target_fails_open(target_db):
    target_db["connect_error"] = psycopg.OperationalError("connection timeout expired")
    assert pf.validate_statements(
        1, "ledger", "ro", _stmts("SELECT 1; SELECT 2")) == (True, None, None)


def test_a_literal_the_requester_typed_stays_in_the_refusal(target_db, monkeypatch):
    # With PII masking on, scrub() hides a value the database read from its own
    # data. A literal in the requester's own SQL is not one: they need it to find
    # the bad value.
    monkeypatch.setattr("queryhub.pii.is_enabled", lambda: True)
    monkeypatch.setattr("queryhub.pii.mask_value", lambda value, found: value)
    target_db["on_explain"] = _fail_on(1, pgerr.InvalidTextRepresentation(
        'invalid input syntax for type integer: "abc"'))
    ok, err, _ = pf.validate_statements(
        1, "ledger", "ro", _stmts("SELECT id FROM t WHERE id = 'abc'"))
    assert ok is False
    assert err == 'Invalid input syntax for type integer: "abc"'


def test_a_value_that_is_not_in_the_statement_is_hidden_in_the_refusal(
        target_db, monkeypatch):
    monkeypatch.setattr("queryhub.pii.is_enabled", lambda: True)
    monkeypatch.setattr("queryhub.pii.mask_value", lambda value, found: value)
    target_db["on_explain"] = _fail_on(2, pgerr.InvalidTextRepresentation(
        'invalid input syntax for type integer: "Jordan Ray"'))
    ok, err, _ = pf.validate_statements(
        1, "ledger", "ro", _stmts("SELECT 1; SELECT full_name::int FROM customers"))
    assert ok is False
    assert err == "Statement 2 of 2: Invalid input syntax for type integer: [hidden]"


def test_a_database_that_does_not_exist_is_refused(target_db):
    target_db["connect_error"] = psycopg.OperationalError(
        'connection failed: FATAL:  database "nova" does not exist')
    ok, err, _ = pf.validate_statements(1, "nova", "ro", _stmts("SELECT 1; SELECT 2"))
    assert ok is False and "does not exist" in err


def test_a_spent_budget_lets_the_request_through_without_planning(target_db):
    ok, err, hint = pf.validate_statements(
        1, "ledger", "ro", _stmts("SELECT 1; SELECT 2"), budget_s=0.05)
    assert (ok, err, hint) == (True, None, None)
    assert target_db["conn"].explained == []


# --- what is and is not planned ---------------------------------------------------

def test_set_statements_are_not_planned(target_db):
    pf.validate_statements(
        1, "ledger", "ro", _stmts("SET LOCAL work_mem = '64MB'; SELECT a FROM t; SELECT b FROM u"))
    assert [s.split(") ", 1)[1] for s in target_db["conn"].explained] == [
        "SELECT a FROM t", "SELECT b FROM u"]


def test_checking_stops_at_the_first_ddl_statement(target_db):
    stmts = _stmts("INSERT INTO t(a) VALUES (1); ALTER TABLE t ADD COLUMN c int; "
                   "UPDATE t SET c = 1", unrestricted=True)
    assert [s.kind for s in stmts] == ["rw", "ddl", "rw"]
    pf.validate_statements(1, "ledger", "rw", stmts)
    assert [s.split(") ", 1)[1] for s in target_db["conn"].explained] == [
        "INSERT INTO t(a) VALUES (1)"]


@pytest.mark.parametrize("mode", ["ddl", "", "other"])
def test_only_ro_and_rw_are_validated(target_db, mode):
    assert pf.validate_statements(
        1, "ledger", mode, _stmts("SELECT 1; SELECT 2")) == (True, None, None)
    assert target_db["connects"] == 0


def test_an_engine_without_explain_is_skipped_without_connecting(target_db):
    target_db["engine"] = "clickhouse"
    assert pf.validate_statements(
        1, "ledger", "ro", _stmts("SELECT 1; SELECT 2")) == (True, None, None)
    assert target_db["connects"] == 0


def test_nothing_to_plan_means_no_connection(target_db):
    assert pf.validate_statements(1, "ledger", "ro", []) == (True, None, None)
    assert target_db["connects"] == 0


# --- the affected-rows hint -------------------------------------------------------

def test_one_update_returns_the_affected_rows_hint(target_db):
    target_db["plan"] = _update_plan(42)
    ok, err, hint = pf.validate_statements(
        1, "ledger", "rw", _stmts("UPDATE orders SET status = 'x' WHERE id = 5"))
    assert (ok, err) == (True, None)
    assert hint == ":pencil2: Estimate: ~42 rows affected (Update)"


def test_a_script_gets_no_hint(target_db):
    target_db["plan"] = _update_plan(42)
    _, _, hint = pf.validate_statements(
        1, "ledger", "rw", _stmts("UPDATE a SET x = 1 WHERE id = 1; UPDATE b SET y = 2 WHERE id = 2"))
    assert hint is None


def test_the_old_estimate_function_still_gives_the_same_line():
    assert pf._write_estimate_text("DELETE", _update_plan(1200)) == (
        ":pencil2: Estimate: ~1.2K rows affected (Delete)")
    assert pf._write_estimate_text("DELETE", None) is None


# --- one statement per message, enforced by the server ------------------------------
#
# A READ ONLY transaction does not stop a tail such as `ROLLBACK; DELETE ...`: the
# tail ends the transaction and the DELETE runs in a new, writable one (measured on
# PostgreSQL 18). The extended protocol makes the SERVER refuse any text that holds
# more than one statement, so no parser disagreement can smuggle a tail past it.

def test_every_explain_goes_over_the_extended_protocol(target_db):
    target_db["plan"] = _update_plan(3)
    pf.validate_statements(1, "ledger", "ro", _stmts("SELECT 1; SELECT 2"))
    pf.validate_statements(1, "ledger", "rw", _stmts("UPDATE t SET a = 1 WHERE id = 1"))
    assert target_db["conn"].explain_kwargs == [{"prepare": True}]
    pf.explain_write_estimate(1, "ledger", "rw", "UPDATE t SET a = 1 WHERE id = 1")
    assert target_db["conn"].explain_kwargs == [{"prepare": True}]
    pf.explain(1, "ledger", "ro", "SELECT 1")
    assert target_db["conn"].explain_kwargs == [{"prepare": True}]


def test_a_text_with_two_commands_is_refused_and_logged(target_db, caplog):
    target_db["on_explain"] = _fail_on(1, pgerr.SyntaxError(
        "cannot insert multiple commands into a prepared statement"))
    with caplog.at_level("WARNING", logger="queryhub.pre_flight"):
        ok, err, _ = pf.validate_statements(
            1, "ledger", "rw", _stmts("UPDATE t SET a = 1 WHERE id = 1"))
    assert ok is False and "multiple commands" in err
    assert any("more than one command" in r.getMessage() for r in caplog.records)


# --- a comment in front of the statement ------------------------------------------
#
# 5 RW requests in 90 days started with a comment. None of them got an estimate:
# the old check read the first white-space word, which was `--`, and skipped the
# statement without a sound. It was not a privilege problem.

@pytest.mark.parametrize("sql,word", [
    ("-- fix the status\nUPDATE t SET a = 1", "UPDATE"),
    ("/* why */ DELETE FROM t WHERE id = 1", "DELETE"),
    ("-- a\n-- b\n  INSERT INTO t VALUES (1)", "INSERT"),
    ("  (SELECT 1)", "SELECT"),
    ("UPDATE t SET a = 1", "UPDATE"),
    ("", ""),
])
def test_the_leading_word_skips_comments(sql, word):
    assert pf._leading_word(sql) == word


def test_a_comment_does_not_hide_what_can_be_planned():
    assert pf.is_explainable("-- c\nSELECT 1") is True
    assert pf.is_explainable("/* c */ UPDATE t SET a = 1 WHERE id = 1") is True


def test_a_comment_does_not_make_an_explain_or_a_create_explainable():
    # `-- c\nEXPLAIN SELECT 1` used to be wrapped in a second EXPLAIN and refused.
    assert pf.is_explainable("-- c\nEXPLAIN SELECT 1") is False
    assert pf.is_explainable("/* c */ CREATE TABLE t (a int)") is False
    assert pf.is_explainable("-- c\nSHOW work_mem") is False


def test_the_old_estimate_function_sees_through_a_leading_comment(target_db):
    target_db["plan"] = _update_plan(42)
    hint = pf.explain_write_estimate(
        1, "ledger", "rw", "-- fix\nUPDATE orders SET status = 'x' WHERE id = 5")
    assert hint == ":pencil2: Estimate: ~42 rows affected (Update)"


def test_validation_plans_a_statement_that_starts_with_a_comment(target_db):
    target_db["plan"] = _update_plan(7)
    sql = "-- fix the status\nUPDATE orders SET status = 'x' WHERE id = 5"
    ok, err, hint = pf.validate_statements(1, "ledger", "rw", _stmts(sql))
    assert (ok, err) == (True, None)
    assert hint == ":pencil2: Estimate: ~7 rows affected (Update)"
    assert len(target_db["conn"].explained) == 1
    assert "-- fix the status" in target_db["conn"].explained[0]


def test_a_typo_behind_a_comment_is_refused(target_db):
    target_db["on_explain"] = _fail_on(
        1, pgerr.UndefinedColumn('column "stauts" of relation "orders" does not exist'))
    sql = "-- fix the status\nUPDATE orders SET stauts = 'x' WHERE id = 5"
    ok, err, _ = pf.validate_statements(1, "ledger", "rw", _stmts(sql))
    assert ok is False and "stauts" in err


# --- core_submit wiring -----------------------------------------------------------

PLAIN = "U0EXAMPLE002"
RW_SQL = "UPDATE orders SET status = 'x' WHERE id = 5"
RO_ONE = "SELECT id FROM orders WHERE id = 5"
RO_TWO = "SELECT id FROM orders WHERE id = 5; SELECT status FROM orders WHERE id = 6"


@pytest.fixture
def submit(monkeypatch):
    """validate_submission up to a Prepared with pre-flight ON and its four
    entry points replaced by recorders. `calls` lists what was invoked."""
    calls: list[str] = []
    state = {"validate": (True, None, None), "validate_raises": False,
             "switch": True, "explain": (True, None, None)}

    def validate(target_id, database, mode, statements):
        calls.append(f"validate:{mode}:{len(statements)}")
        if state["validate_raises"]:
            raise RuntimeError("boom")
        return state["validate"]

    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    # An admin is exempt from the in-flight cap, which keeps the DB out of it.
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(cs.replicas, "replicas_of", lambda pid: [])
    monkeypatch.setattr(cs.targets, "get", lambda tid: targets.TargetServer(
        id=7, alias="prod-ledger", host="primary.example.test", port=5432,
        default_database="ledger", username="reader", enabled=True, notes=None,
        engine="postgres"))
    monkeypatch.setattr(cs.teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "rw"})
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "rw")
    monkeypatch.setattr(cs.auto_approve, "effective_grant", lambda *a, **k: None)
    monkeypatch.setattr(cs.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: True)
    monkeypatch.setattr(cs.pre_flight, "is_plan_logging_enabled", lambda: False)
    monkeypatch.setattr(cs.pre_flight, "script_validation_enabled", lambda: state["switch"])
    monkeypatch.setattr(cs.pre_flight, "validate_statements", validate)
    monkeypatch.setattr(cs.pre_flight, "explain", lambda *a, **k: (
        calls.append("explain") or state["explain"]))
    monkeypatch.setattr(cs.pre_flight, "explain_write_estimate", lambda *a, **k: (
        calls.append("estimate") or ":pencil2: old estimate"))

    def go(sql):
        return cs.validate_submission(
            PLAIN, "Ex", target_server_id=7, database_name=None, query=sql,
            justification="because")

    go.calls, go.state = calls, state
    return go


def test_a_wrong_column_in_an_rw_request_is_refused_before_approval(submit):
    submit.state["validate"] = (False, 'Column "stauts" does not exist', None)
    out = submit(RW_SQL)
    assert isinstance(out, cs.Rejection) and out.field == "query"
    assert out.message == 'Query failed validation: Column "stauts" does not exist'
    assert submit.calls == ["validate:rw:1"]


def test_a_wrong_column_in_a_multi_statement_ro_request_is_refused(submit):
    submit.state["validate"] = (False, 'Statement 2 of 2: Column "x" does not exist', None)
    out = submit(RO_TWO)
    assert isinstance(out, cs.Rejection) and out.field == "query"
    assert "Statement 2 of 2" in out.message
    # explain() skips a script by itself; the script check is the one that counts.
    assert "validate:ro:2" in submit.calls


def test_a_single_ro_statement_keeps_using_explain_only(submit):
    out = submit(RO_ONE)
    assert isinstance(out, cs.Prepared)
    assert submit.calls == ["explain"]


def test_the_rw_hint_reaches_the_approver_card(submit):
    submit.state["validate"] = (True, None, ":pencil2: Estimate: ~3 rows affected (Update)")
    out = submit(RW_SQL)
    assert isinstance(out, cs.Prepared)
    assert out.risk_summary == ":pencil2: Estimate: ~3 rows affected (Update)"


def test_a_crash_in_the_check_never_blocks_a_request(submit):
    submit.state["validate_raises"] = True
    out = submit(RW_SQL)
    assert isinstance(out, cs.Prepared)


def test_the_kill_switch_restores_the_old_rw_behaviour(submit):
    submit.state["switch"] = False
    out = submit(RW_SQL)
    assert isinstance(out, cs.Prepared)
    assert submit.calls == ["estimate"]
    assert out.risk_summary == ":pencil2: old estimate"


def test_the_kill_switch_also_skips_the_multi_statement_check(submit):
    submit.state["switch"] = False
    out = submit(RO_TWO)
    assert isinstance(out, cs.Prepared)
    assert not any(c.startswith("validate") for c in submit.calls)
