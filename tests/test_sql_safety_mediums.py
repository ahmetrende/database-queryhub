"""SQL-boundary gaps: quoted function names, MERGE, and the MSSQL escape hatch.

- A QUOTED function call — `"pg_read_file"(...)`, which PostgreSQL resolves to
  the same function — put an exp.Identifier where the code expected a string.
  One reader skipped it (so quoting a name walked past the dangerous-function
  blocklist) and another raised AttributeError (so the submit crashed).
- MERGE is a write whose row selector is ON, not WHERE, so it skipped both the
  WHERE-required and always-true guards: `ON true` touches every matched row.
- ast_safety_enabled is a reasonable false-positive escape hatch on PostgreSQL,
  where many other guards remain. On SQL Server it is the ONLY defense against
  batch smuggling / OPENROWSET / linked servers, so honoring "off" there was a
  one-click full bypass.
"""
import pytest

from dba_slack_bot import ast_safety
from dba_slack_bot import config as cfg
from dba_slack_bot import query_safety as qs


@pytest.mark.parametrize("sql", [
    "SELECT pg_read_file('/etc/passwd')",
    'SELECT "pg_read_file"(\'/etc/passwd\')',
    'SELECT "lo_export"(1, \'/tmp/x\')',
    'SELECT "dblink_exec"(\'\', \'DROP TABLE t\')',
])
def test_quoted_and_bare_dangerous_functions_are_blocked(sql):
    report = qs.analyze(sql, engine="postgres")
    assert report.blocked, f"dangerous function slipped through: {sql}"


def test_quoted_function_call_does_not_crash_analysis():
    # Regression: this raised AttributeError('Identifier' has no 'lower').
    qs.analyze('SELECT "pg_sleep"(9999)', engine="postgres")
    qs.analyze('SELECT "set_config"(\'work_mem\',\'64MB\',true)', engine="postgres")


def test_anon_name_resolves_both_forms():
    import sqlglot
    from sqlglot import exp
    for sql in ["SELECT pg_read_file('x')", 'SELECT "pg_read_file"(\'x\')']:
        call = sqlglot.parse_one(sql, read="postgres").find(exp.Anonymous)
        assert ast_safety.anon_name(call) == "pg_read_file"


@pytest.mark.parametrize("sql", [
    "MERGE INTO t USING s ON true WHEN MATCHED THEN UPDATE SET a = 1",
    "MERGE INTO t USING s ON 1=1 WHEN MATCHED THEN UPDATE SET a = 1",
])
def test_merge_with_always_true_on_is_blocked(sql):
    report = qs.analyze(sql, engine="postgres")
    assert report.blocked
    assert any("ON condition" in b for b in report.blockers), report.blockers


@pytest.mark.parametrize("sql", [
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = 1",
    "MERGE INTO t USING s ON t.id = s.id AND t.v > 0 WHEN MATCHED THEN DELETE",
])
def test_merge_with_a_real_condition_is_allowed(sql):
    report = qs.analyze(sql, engine="postgres")
    assert not report.blocked, report.blockers


def test_mssql_ignores_the_ast_safety_toggle(monkeypatch):
    monkeypatch.setattr(cfg, "get_setting",
                        lambda k, d=None: "off" if k == "ast_safety_enabled" else d)
    assert ast_safety.is_enabled("mssql") is True
    # ...and the MSSQL-only protections still fire.
    for sql in ["SELECT * FROM OPENROWSET('SQLNCLI','x','SELECT 1')",
                "SELECT * FROM linked.remote.dbo.t"]:
        assert qs.analyze(sql, engine="mssql").blocked, sql


def test_postgres_still_honors_the_toggle(monkeypatch):
    # The escape hatch must keep working where it is genuinely a second opinion.
    monkeypatch.setattr(cfg, "get_setting",
                        lambda k, d=None: "off" if k == "ast_safety_enabled" else d)
    assert ast_safety.is_enabled("postgres") is False
    assert qs.analyze("SELECT pg_read_file('/etc/passwd')",
                      engine="postgres").blocked is False


# --- IS NOT NULL has two AST spellings -------------------------------------
# The always-true guard recognised `x IS NULL OR x IS NOT NULL` by looking for
# an outer exp.Not wrapping an exp.Is. sqlglot 30.11 moved that negation onto
# the Is node as `negate=True`, so on a newer parser both operands read as the
# same plain `IS NULL`, the tautology went unnoticed, and
# `UPDATE t SET a=1 WHERE id IS NOT NULL OR id IS NULL` — a full-table write —
# stopped being blocked. The SQL corpus only ever exercises the ONE spelling
# the installed sqlglot happens to produce, which is why a pinned dev box
# stayed green while CI went red. These build both shapes by hand so the guard
# is verified against each on every run, whatever version is installed.

def _is_null(col, negation):
    """`col IS NULL`, or `col IS NOT NULL` in the requested spelling."""
    from sqlglot import exp
    node = exp.Is(this=exp.column(col), expression=exp.Null())
    if negation == "negate-arg":            # sqlglot >= 30.11
        node.set("negate", True)
        return node
    if negation == "not-wrapper":           # sqlglot <= 30.10
        return exp.Not(this=node)
    return node


@pytest.mark.parametrize("negation", ["negate-arg", "not-wrapper"])
def test_or_of_is_null_and_is_not_null_is_caught_in_both_spellings(monkeypatch, negation):
    import sqlglot
    from sqlglot import exp

    predicate = exp.Or(this=_is_null("id", negation), expression=_is_null("id", None))
    tree = sqlglot.parse_one("SELECT 1 WHERE FALSE", read="postgres")
    tree.set("where", exp.Where(this=predicate))
    monkeypatch.setattr(sqlglot, "parse_one", lambda *a, **k: tree)

    assert qs._where_ast_has_no_row_filter("id IS NOT NULL OR id IS NULL") is True


@pytest.mark.parametrize("sql", [
    "UPDATE t SET a=1 WHERE id IS NOT NULL OR id IS NULL",
    "UPDATE t SET a=1 WHERE id IS NULL OR id IS NOT NULL",
    "DELETE FROM t WHERE users.id IS NULL OR users.id IS NOT NULL",
])
def test_a_null_check_ored_with_its_negation_blocks_the_write(sql):
    assert qs.analyze(sql, engine="postgres").blocked, f"not blocked: {sql}"


def test_a_real_null_check_still_filters(sql=None):
    # The guard must not swing the other way: one-sided NULL tests are honest
    # predicates, and so is a NULL test OR'd with a different column's.
    for ok in ["UPDATE t SET a=1 WHERE id IS NOT NULL",
               "UPDATE t SET a=1 WHERE id IS NULL",
               "DELETE FROM t WHERE id IS NULL OR name IS NOT NULL"]:
        assert not qs.analyze(ok, engine="postgres").blocked, ok
