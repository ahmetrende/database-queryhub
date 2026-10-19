"""A SELECT that calls `setval` or `nextval` is a write.

Requests 11826 and 11827 (2026-10-19) were `SELECT setval(...)` from a
super-admin. The leading word said SELECT, so the tier was `ro`, the read-only
login ran it, and the server refused it: "Permission denied for sequence". The
fault was the tier, not the grant. These tests pin the rule and its edges.
"""
import pytest

from queryhub import query_safety


@pytest.mark.parametrize("sql", [
    "SELECT setval(pg_get_serial_sequence('states', 'id'), (SELECT MAX(id) FROM states));",
    "SELECT pg_catalog.setval('s', 5)",
    "SELECT nextval('s')",
    "SELECT NEXTVAL('s'::regclass)",
    'SELECT "setval"(\'s\', 1)',
    "SELECT * FROM t WHERE id = nextval('s')",
    "WITH x AS (SELECT nextval('s')) SELECT * FROM x",
])
def test_a_select_that_changes_a_sequence_needs_the_write_tier(sql):
    assert query_safety.analyze(sql).main_tier == "rw"
    assert query_safety.analyze(sql, unrestricted=True).main_tier == "rw"


@pytest.mark.parametrize("sql", [
    "SELECT currval('s')",
    "SELECT lastval()",
    "SELECT 'setval' AS note",
    "SELECT id FROM states WHERE name = 'nextval'",
    "SELECT * FROM states",
])
def test_a_read_that_only_mentions_the_word_stays_read_only(sql):
    assert query_safety.analyze(sql).main_tier == "ro"


def test_plain_explain_does_not_run_the_call_and_keeps_its_tier():
    assert query_safety.analyze("EXPLAIN SELECT nextval('s')").main_tier == "ro"


def test_another_engine_is_not_affected():
    """Sequences work differently on SQL Server. The rule is Postgres only."""
    assert query_safety.analyze("SELECT nextval('s')", engine="mssql").main_tier == "ro"


def test_a_statement_that_is_already_a_write_keeps_its_tier():
    assert query_safety.analyze("UPDATE t SET a = nextval('s') WHERE id = 1").main_tier == "rw"
    assert query_safety.analyze("CREATE TABLE x AS SELECT nextval('s')").main_tier == "ddl"


def test_required_mode_reports_the_same_tier():
    sql = "SELECT setval('s', 5)"
    assert query_safety.required_mode(sql) == "rw"
