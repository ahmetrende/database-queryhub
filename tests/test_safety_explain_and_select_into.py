"""Two gaps in the safety layer, from the v2 audit backlog.

**Plain EXPLAIN never had its inner statement scanned.** sqlglot parses
`EXPLAIN ...` as an opaque Command, so the module-level AST check cannot see
inside it. `EXPLAIN ANALYZE` grew its own inner scan when that path was hardened;
plain `EXPLAIN` did not, and `EXPLAIN SELECT pg_read_file('/etc/passwd')` passed
every gate while the bare call was blocked.

Plain EXPLAIN plans without executing, so a VOLATILE function is not called —
which is an argument about volatility, not about this code. An IMMUTABLE function
with constant arguments IS folded at plan time, and the blocklist is not curated
by volatility, so the two EXPLAIN forms now answer the same way.

**`SELECT ... INTO t` was classified `ro`.** It leads with SELECT and it creates a
table: Postgres treats it as CREATE TABLE AS, T-SQL the same. The read-only
credential would have refused it, which is the backstop — but the approver was
shown a read, and `CREATE TABLE AS`, the identical operation written differently,
has always been ddl.
"""
import pytest

from dba_slack_bot import query_safety as qs


def _report(sql, engine="postgres"):
    return qs.analyze(sql, engine=engine)


# ---------------------------------------------------------------------------
# plain EXPLAIN
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "EXPLAIN SELECT pg_read_file('/etc/passwd');",
    "EXPLAIN (BUFFERS) SELECT pg_read_file('/etc/passwd');",
    "EXPLAIN (FORMAT JSON, BUFFERS) SELECT dblink('h', 'select 1');",
    "EXPLAIN SELECT lo_import('/etc/shadow');",
])
def test_a_blocked_function_cannot_ride_in_on_plain_explain(sql):
    r = _report(sql)
    assert r.blocked is True, (
        "EXPLAIN wrapped a blocked function and the AST check never saw it")
    assert any("blocked" in b.lower() for b in r.blockers), r.blockers


@pytest.mark.parametrize("sql", [
    "EXPLAIN SELECT id FROM orders WHERE id = 1;",
    "EXPLAIN (FORMAT JSON) SELECT 1;",
    "EXPLAIN (COSTS off, BUFFERS) SELECT count(*) FROM orders;",
])
def test_an_ordinary_explain_still_passes(sql):
    """The other half. A gate that blocks EXPLAIN outright would be useless —
    pre-flight uses it on every submit."""
    assert _report(sql).blocked is False


def test_analyze_false_is_blocked_too_and_that_is_deliberate():
    """`EXPLAIN (ANALYZE false)` does NOT execute, and it is blocked anyway.

    The gate matches the WORD, not the option's value. That is a false positive,
    and it is the direction to be wrong in: teaching a security gate to parse
    option values means the gate can now be wrong about `ANALYZE off`,
    `ANALYZE 0`, `ANALYZE FALSE` and whatever the next Postgres release accepts.
    Recorded as a test so nobody "fixes" it without deciding to.
    """
    assert _report("EXPLAIN (ANALYZE false) SELECT 1;").blocked is True


def test_explain_analyze_keeps_its_own_stricter_gate():
    """ANALYZE executes the wrapped statement, so a write must stay blocked
    regardless of what the plain-EXPLAIN path now does."""
    for sql in ("EXPLAIN ANALYZE DELETE FROM orders;",
                "EXPLAIN ANALYSE DELETE FROM orders;",     # British spelling
                "EXPLAIN (ANALYZE) UPDATE orders SET x = 1;"):
        assert _report(sql).blocked is True, sql


# ---------------------------------------------------------------------------
# SELECT ... INTO
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("engine", ["postgres", "mssql"])
def test_select_into_is_ddl_not_ro(engine):
    r = _report("SELECT * INTO new_tbl FROM orders;", engine=engine)
    assert r.main_tier == "ddl", (
        "a table-creating statement was classified as a read, so the approval "
        "card showed RO and the read-only credential was selected")


def test_it_matches_create_table_as():
    """Same operation, two spellings, one tier — that is the whole point."""
    a = _report("SELECT * INTO t2 FROM orders;").main_tier
    b = _report("CREATE TABLE t2 AS SELECT * FROM orders;").main_tier
    assert a == b == "ddl"


@pytest.mark.parametrize("sql", [
    "SELECT id FROM orders WHERE id = 1;",
    "SELECT id FROM orders WHERE id IN (1, 2, 3);",              # IN, not INTO
    "SELECT id FROM orders WHERE id IN (SELECT x FROM t);",      # nested SELECT
    "SELECT 'insert into x' AS s;",                              # INTO in a literal
    "SELECT count(*) FROM orders;",
])
def test_an_ordinary_read_stays_ro(sql):
    """False positives here cost a user a DDL approval on a read. The direction
    is safe but the annoyance is real, so the detector has to be precise about
    what it is not."""
    assert _report(sql).main_tier == "ro", sql


def test_insert_into_is_unaffected():
    assert _report("INSERT INTO orders VALUES (1);").main_tier == "rw"


def test_the_detector_ignores_a_subquery_level_into():
    """Depth matters: only a TOP-LEVEL INTO creates the table this statement is
    classified for."""
    assert qs._selects_into("SELECT a FROM (SELECT b INTO x FROM t) q") is False
    assert qs._selects_into("SELECT a INTO x FROM t") is True
