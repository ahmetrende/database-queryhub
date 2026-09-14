"""The column-name PII catalog must survive derived tables and CTEs.

`SELECT a FROM (SELECT full_name AS a FROM customers) t` renames the column, so
matching output names against the catalog found nothing and the catalog stopped
applying entirely — every name shipped in clear. The fix resolves each output
column through the query's alias definitions, transitively, so the physical
column behind the alias is checked as well.
"""
import pytest

from dba_slack_bot import pii


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    # Deterministic catalog: the real one is loaded from the control DB.
    monkeypatch.setattr(pii, "is_enabled", lambda: True)
    monkeypatch.setattr(pii, "_load_column_patterns", lambda: [
        ("full_name", "name"), ("email", "email"), ("tckn", "tckn"),
    ])
    monkeypatch.setattr(
        pii, "_match_pii_type",
        lambda col, pats: next(
            (t for p, t in pats if p in (col or "").lower()), None))


@pytest.mark.parametrize("cols,sql,why", [
    (["full_name"], "SELECT full_name FROM customers", "direct"),
    (["a"], "SELECT a FROM (SELECT full_name AS a FROM customers) t",
     "derived table"),
    (["a"], "WITH c AS (SELECT full_name AS a FROM customers) SELECT a FROM c",
     "CTE"),
    (["b"], "SELECT b FROM (SELECT a AS b FROM (SELECT email AS a FROM users) i) o",
     "two levels of nesting"),
    (["x"], "SELECT x FROM (SELECT tckn::text AS x FROM people) t",
     "cast inside a subquery"),
])
def test_catalog_follows_aliases(cols, sql, why):
    assert pii.column_pii_map(cols, sql), f"catalog lost the column via {why}"


@pytest.mark.parametrize("cols,sql", [
    (["n"], "SELECT n FROM (SELECT count(*) AS n FROM orders) t"),
    (["id", "note"], "SELECT id, note FROM tickets"),
    (["total"], "SELECT sum(amount) AS total FROM payments"),
])
def test_non_pii_queries_stay_unmasked(cols, sql):
    # Over-masking is its own failure: it corrupts legitimate results.
    assert pii.column_pii_map(cols, sql) == {}


def test_self_referential_alias_does_not_hang():
    # `x AS x` makes the alias graph cyclic; the closure must terminate.
    assert pii._expand_aliases({"x"}, {"x": {"x"}}) == {"x"}


def test_alias_expansion_is_transitive():
    out = pii._expand_aliases({"c"}, {"c": {"b"}, "b": {"a"}, "a": {"full_name"}})
    assert "full_name" in out


# ---------------------------------------------------------------------------
# The parser above closes aliases, casts, derived tables and CTEs. It cannot
# close a VIEW: a view's body is not in the submitted SQL, it is in the
# catalog. Measured on the live database 2026-07-30 —
#
#     CREATE VIEW v_cust AS
#       SELECT full_name AS disp, address AS loc, email AS contact FROM customers;
#     SELECT disp, loc, contact FROM v_cust;   -->  column_pii_map == {}
#
# nothing was masked. The planner already knows, and says so in
# EXPLAIN (VERBOSE, FORMAT JSON, COSTS OFF), whose root `Output` is positionally
# identical to the result columns. The end-to-end proof needs a real database
# and lives in the integration suite; below are the parts that fail silently.
# ---------------------------------------------------------------------------
import pathlib

from dba_slack_bot import pii_lineage


def test_root_output_comes_from_the_outermost_node_that_has_one():
    plan = [{"Plan": {"Node Type": "Limit", "Output": ["c.full_name", "c.email"],
                      "Plans": [{"Node Type": "Seq Scan", "Output": ["c.id"]}]}}]
    assert pii_lineage._pg_output_array(plan) == ["c.full_name", "c.email"]


def test_a_node_without_an_output_delegates_to_its_children():
    """Some Append / Gather shapes carry no Output of their own; stopping there
    would silently lose the lineage for a partitioned table."""
    plan = [{"Plan": {"Node Type": "Append",
                      "Plans": [{"Node Type": "Seq Scan", "Output": ["p.birth_date"]}]}}]
    assert pii_lineage._pg_output_array(plan) == ["p.birth_date"]


def test_a_shapeless_plan_yields_nothing_rather_than_raising():
    for junk in (None, [], {}, [{}], "nope", [{"Plan": {}}]):
        assert pii_lineage._pg_output_array(junk) is None


def test_an_expression_offers_every_identifier_in_it():
    """An Output entry is an expression, not always a bare reference. Extra
    candidates only ever cause MORE catalog checking, so breadth is safe and
    precision is not worth the risk of missing one."""
    assert "email" in pii_lineage._names_in("upper((c.email)::text)")
    got = pii_lineage._names_in("(c.first || ' '::text)")
    assert "first" in got and "text" not in got


def test_only_result_producing_reads_are_planned():
    """DML is excluded deliberately: a RETURNING clause names its columns in the
    statement text, which the static resolver already reads, so there is nothing
    to gain and one more thing to go wrong on a write path."""
    w = pii_lineage._LINEAGE_WORTHY
    assert w.match("SELECT 1") and w.match("  with x as (select 1) select * from x")
    assert not w.match("UPDATE t SET a = 1 RETURNING a")
    assert not w.match("INSERT INTO t VALUES (1)")
    assert not w.match("CREATE TABLE t (a int)")


def test_an_unimplemented_engine_is_declined_not_guessed():
    """SQL Server has an equivalent (SHOWPLAN_XML emits ColumnReference); it is
    unimplemented, so it must degrade to the static path rather than pretend."""
    assert pii_lineage.supported("postgres")
    assert not pii_lineage.supported("mssql")
    assert pii_lineage.source_columns(None, "SELECT 1", engine="mssql") is None
    assert pii_lineage.source_columns(None, "", engine="postgres") is None


def test_the_explain_runs_in_a_savepoint_and_always_rolls_back():
    """Both halves matter and neither shows from outside. A failed statement
    aborts a Postgres transaction, so an EXPLAIN this module cannot plan would
    otherwise take the user's real query with it ("current transaction is
    aborted") — a masking improvement turning into a query-killer. Rolling back
    on SUCCESS too is what stops the 5s planning budget (SET LOCAL) leaking onto
    a legitimately long main statement."""
    src = pathlib.Path(pii_lineage.__file__).read_text(encoding="utf-8")
    body = src[src.index("def _postgres"):]
    assert "SAVEPOINT qh_pii_lineage" in body
    assert "SET LOCAL statement_timeout" in body
    assert "finally:" in body
    assert body.index("finally:") < body.index("ROLLBACK TO SAVEPOINT")


def test_lineage_is_resolved_before_any_portal_opens():
    """The 2026-07-30 outage as a test: issuing a second statement while
    `cur.stream()` holds a portal open blocks forever with no client-side
    timeout — and what caused it was a catalog lookup exactly like this one."""
    src = (pathlib.Path(pii_lineage.__file__).parent / "executor.py") \
        .read_text(encoding="utf-8")
    i_lineage = src.index("pii_lineage.source_columns(")
    assert i_lineage < src.index("stream = cur.stream(stmt.rewritten)")
    assert i_lineage < src.index("cur.execute(stmt.rewritten)")


def test_arity_disagreement_is_refused_rather_than_mis_mapped():
    """Positional identity is the whole premise. A wrong mapping is worse than
    none: it would mask an innocent column and leave the real one alone."""
    assert pii_lineage._pg_output_array([{"Plan": {"Output": ["a", "b"]}}]) == ["a", "b"]
    # the arity guard itself lives in _postgres; pin the log-and-drop intent
    src = pathlib.Path(pii_lineage.__file__).read_text(encoding="utf-8")
    assert "arity" in src and "return None" in src


def test_lineage_cannot_write_past_the_end_of_the_row():
    cols = ["a"]
    out = pii.column_pii_map(cols, None, lineage=[{"x"}, {"full_name"}, {"email"}])
    assert all(i < len(cols) for i in out)
