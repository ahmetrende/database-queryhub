"""Three things a super-admin does in a desktop client, and now does here.

Operator request, 2026-09-26: as a super-admin QueryHub has to work like
DataGrip, pgAdmin or SSMS. Three gaps were measured the same day and are closed
for the super-admin path only; for everyone else each is refused exactly as
before, and the tests come in pairs so neither side can drift.

1. Ending or cancelling a session (pg_terminate_backend, pg_cancel_backend). It
   asks first and runs at the ddl tier, because the read-only login cannot
   signal another role's backend and the elevated role can.
2. A script that mixes tiers (a SELECT, an UPDATE, a SELECT) runs as one
   request at its highest tier. The mixed-tier refusal exists so an approver
   sees each statement at its own tier; a super-admin's request has none.
3. `SET search_path` to a list of schema names, kept to the one request by
   the rewrite to SET LOCAL.
"""
import pytest

from queryhub import query_safety as qs


def _super(sql, **kw):
    return qs.analyze(sql, unrestricted=True, **kw)


# --- 1. ending a session -------------------------------------------------------

KILLS = [
    "SELECT pg_terminate_backend(12345)",
    "SELECT pg_cancel_backend(12345)",
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE state = 'idle in transaction' AND xact_start < now() - interval '1 hour'",
]


@pytest.mark.parametrize("sql", KILLS)
def test_everyone_else_is_still_refused(sql):
    r = qs.analyze(sql)
    assert r.blocked and "is blocked by the QueryHub safety policy" in r.blockers[0]


@pytest.mark.parametrize("sql", KILLS)
def test_a_super_admin_is_asked_and_runs_it_as_the_elevated_role(sql):
    r = _super(sql)
    assert not r.blocked
    assert r.main_tier == "ddl", "only the elevated role may signal another backend"
    assert r.needs_confirmation and len(r.confirmations) == 1


def test_the_question_says_what_each_one_costs():
    term = _super("SELECT pg_terminate_backend(1)").confirmations[0]
    cancel = _super("SELECT pg_cancel_backend(1)").confirmations[0]
    assert "disconnected" in term and "rolled back" in term and "lost" in term
    assert "stopped" in cancel and "disconnected" not in cancel


@pytest.mark.parametrize("sql", [
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_drop_replication_slot('dms_slot')",
    "SELECT pg_reload_conf()",
    "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
    "EXPLAIN SELECT pg_terminate_backend(1)",
])
def test_nothing_else_on_the_list_opens_with_it(sql):
    assert _super(sql).blocked, sql


def test_an_operator_block_still_wins(monkeypatch):
    """bot_config postgres_blocked_functions only ever tightens."""
    from queryhub import ast_safety
    monkeypatch.setattr(ast_safety.cfg, "get_setting",
                        lambda k, d=None: "pg_cancel_backend"
                        if k == "postgres_blocked_functions" else d)
    assert _super("SELECT pg_cancel_backend(1)").blocked
    assert not _super("SELECT pg_terminate_backend(1)").blocked


def test_only_postgres_treats_the_names_as_session_control():
    """On SQL Server the name is just an unknown function; it stays refused."""
    assert qs.analyze("SELECT pg_terminate_backend(1)", engine="mssql",
                      unrestricted=True).blocked


# --- 2. a mixed-tier script ----------------------------------------------------

FIX_AND_CHECK = ("SELECT status FROM orders WHERE id = 42; "
                 "UPDATE orders SET status = 'cancelled' WHERE id = 42; "
                 "SELECT status FROM orders WHERE id = 42;")


def test_everyone_else_still_splits_a_mixed_script():
    r = qs.analyze(FIX_AND_CHECK)
    assert r.blocked and "Mixed-tier submission rejected" in r.blockers[0]


def test_a_super_admin_script_runs_at_its_highest_tier():
    r = _super(FIX_AND_CHECK)
    assert not r.blocked and r.main_tier == "rw"
    assert [s.kind for s in r.statements] == ["ro", "rw", "ro"], \
        "each statement keeps its own kind in the record"
    assert _super("SELECT 1; CREATE TABLE t (id int);").main_tier == "ddl"


def test_a_mixed_script_is_still_checked_statement_by_statement():
    assert _super("SELECT 1; SELECT pg_read_file('/etc/passwd');").blocked
    assert _super("SELECT 1; UPDATE orders SET status = 'x';").needs_confirmation


# --- 3. SET search_path --------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "SET search_path = app, public; SELECT * FROM orders",
    'SET search_path TO "$user", public; SELECT 1',
    "SET search_path = 'app', public; SELECT 1",
])
def test_a_super_admin_may_name_their_schemas(sql):
    r = _super(sql)
    assert not r.blocked, r.blockers
    prelude = r.statements[0]
    assert prelude.kind == "set" and prelude.rewritten.upper().startswith("SET LOCAL ")


def test_everyone_else_is_still_refused_search_path():
    r = qs.analyze("SET search_path = app, public; SELECT * FROM orders")
    assert r.blocked and "search_path" in r.blockers[0]


@pytest.mark.parametrize("sql", [
    "SET search_path = app, (SELECT 1); SELECT 1",
    "SET search_path = app, public, pg_catalog_x' ; SELECT 1",
    "SET search_path = " + ", ".join(f"s{i}" for i in range(20)) + "; SELECT 1",
    "SET SESSION search_path = app; SELECT 1",
])
def test_only_a_list_of_names_is_accepted(sql):
    assert _super(sql).blocked, sql


# --- the default path is untouched -----------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT 1", "UPDATE orders SET status = 'x' WHERE id = 1",
    "CREATE TABLE t (id int)", "SET work_mem = '64MB'; SELECT 1",
])
def test_a_statement_both_modes_accept_keeps_its_tier(sql):
    assert qs.analyze(sql).main_tier == _super(sql).main_tier
