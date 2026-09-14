"""The approved text must be the executed text.

sqlparse (which analyze() classifies with) accepts `\\'` as an escaped quote
— a MySQL convention. PostgreSQL with standard_conforming_strings=on and
T-SQL both read `'\\'` as a COMPLETE one-character string. So a payload like

    UPDATE t SET a = '\\'; DELETE FROM t; --' WHERE id = 5

looks like ONE filtered UPDATE to the classifier, the approval card and the
DBA, while the server runs THREE statements: an UNFILTERED update plus a
hidden DELETE. Every guard is defeated at once (multi-statement, mixed-tier,
UPDATE-without-WHERE) and the audit records n_statements=1.

analyze() now cross-checks its decomposition against the engine's own parser
and fails closed on disagreement. The executor additionally runs Postgres
statements over the extended protocol, where the server itself refuses a
string containing more than one command.
"""
import pytest

from queryhub import query_safety as qs

SMUGGLING_PAYLOADS = [
    # The canonical form: the backslash swallows the `'` so the real WHERE
    # stays outside the string and reassures the WHERE-required guard.
    ("postgres", r"UPDATE t SET a = '\'; DELETE FROM t; --' WHERE id = 5"),
    # RO tier: a lone SELECT that hides a DDL statement.
    ("postgres", r"SELECT '\'; DROP TABLE t; --'"),
    # Staged exfiltration into a table a later RO request can read.
    ("postgres", r"UPDATE t SET a = '\'; INSERT INTO pub SELECT pw FROM users; --' WHERE id = 1"),
    # T-SQL: EXEC is a banned leading word, so it is smuggled instead.
    ("mssql", r"UPDATE t SET a = '\'; EXEC xp_cmdshell 'whoami'; --' WHERE id = 5"),
]

LEGITIMATE_QUERIES = [
    ("postgres", "SELECT id, email FROM users WHERE id = 5 LIMIT 100"),
    ("postgres", "UPDATE users SET status = 'active' WHERE id = 84213"),
    ("postgres", "SET LOCAL statement_timeout = '30s'; SELECT 1"),
    ("postgres", "WITH x AS (SELECT 1 AS n) SELECT * FROM x"),
    # A doubled quote is the STANDARD escape and must keep working.
    ("postgres", "SELECT 'O''Brien' AS name"),
    # A backslash that is NOT adjacent to a quote is ordinary text in both
    # parsers, so a Windows path must not trip the gate.
    ("postgres", r"SELECT 'C:\path\to\file' AS p"),
    ("postgres", "CREATE INDEX CONCURRENTLY ix_u ON users(email)"),
    ("mssql", "SELECT TOP 10 * FROM dbo.FactTrades"),
]


@pytest.mark.parametrize("engine,sql", SMUGGLING_PAYLOADS)
def test_smuggled_statements_are_blocked(engine, sql):
    report = qs.analyze(sql, engine=engine)
    assert report.blocked, f"parser-differential payload passed: {sql}"
    assert any("ambiguous" in b for b in report.blockers), report.blockers


@pytest.mark.parametrize("engine,sql", LEGITIMATE_QUERIES)
def test_legitimate_sql_still_passes(engine, sql):
    report = qs.analyze(sql, engine=engine)
    assert not report.blocked, f"false positive on {sql}: {report.blockers}"


def test_gate_is_independent_of_ast_safety_toggle(monkeypatch):
    # The cross-check protects the product's central invariant, so it must NOT
    # be disabled by the operator-facing ast_safety_enabled escape hatch.
    from queryhub import ast_safety
    monkeypatch.setattr(ast_safety, "check", lambda sql, engine="postgres": [])
    report = qs.analyze(r"UPDATE t SET a = '\'; DELETE FROM t; --' WHERE id = 5",
                        engine="postgres")
    assert report.blocked


def test_plain_multi_statement_is_still_caught_by_its_own_guard():
    # Regression guard for the pre-existing behaviour the differential bypassed.
    report = qs.analyze("UPDATE t SET a = 'x'; DELETE FROM t", engine="postgres")
    assert report.blocked


def test_disagreement_helper_ignores_unparseable_sql():
    # sqlglot failing to parse is not evidence of a differential — ast_safety
    # rejects unparseable SQL, and treating a parser gap as a differential
    # here would block legitimate engine-specific syntax.
    from queryhub import engines
    spec = engines.spec("postgres")
    assert qs._statement_count_disagrees("this is not sql at all !!!", spec) is False


# ---------------------------------------------------------------------------
# False positives found in production. The gate is only useful if it does not
# also block ordinary SQL — a developer who cannot append "-- ticket QH-42" to
# an UPDATE will conclude the tool is broken, and they would be right.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT 1; -- trailing comment",
    "SELECT 1;\n-- a note about the query",
    "SELECT 1; /* block comment */",
    "UPDATE t SET a = 1 WHERE id = 1; -- ticket QH-42",
    "/* header */ UPDATE t SET a = 1 WHERE id = 1; -- footer",
    "DELETE FROM t WHERE id = 1;   -- cleanup, approved in QH-99",
])
def test_a_comment_after_the_final_semicolon_is_not_a_differential(sql):
    """sqlglot renders a trailing comment as its own statement node while
    sqlparse keeps it attached, so comparing raw counts flagged every one of
    these. The comparison now ignores comment-only nodes on both sides."""
    report = qs.analyze(sql)
    assert not report.blocked, f"false positive: {sql} -> {report.blockers}"


def test_the_real_differential_is_still_caught_after_that_fix():
    """The payload the gate exists for: sqlparse reads the backslash as an
    escaped quote and sees one statement with a reassuring WHERE; PostgreSQL
    ends the literal and sees a hidden DELETE."""
    sql = r"UPDATE t SET a = '\'; DELETE FROM t; --' WHERE id = 5"
    report = qs.analyze(sql)
    assert report.blocked
    assert any("ambiguous" in b for b in report.blockers)


# ---------------------------------------------------------------------------
# The count-balanced payload. Everything above is caught because the two
# parsers disagree on HOW MANY statements there are. This section is the case
# the count comparison structurally cannot see: exactly one statement either
# way, but the engine's copy has lost its WHERE clause.
#
# Reproduced end-to-end on a real PostgreSQL 2026-07-30 (temp table, rolled
# back): analyze() returned blocked=False, tier=rw, 1 statement, and it SAW
# "WHERE id = 42" — the engine then updated 3 of 3 rows.
# ---------------------------------------------------------------------------

COUNT_BALANCED_PAYLOADS = [
    # The reported one. One statement to both parsers; the trailing --' turns
    # the WHERE into a comment for the server.
    ("postgres", r"UPDATE accounts SET balance = 0, note = '\' --' WHERE id = 42"),
    # Dressed up as a ticket reference, which is what makes it get approved.
    ("postgres", r"UPDATE t SET a = '\' -- ticket QH-42' WHERE id = 1"),
    # Inside a subquery, so the outer statement still looks filtered.
    ("postgres", r"DELETE FROM t USING (SELECT '\') s --' WHERE t.id = 1"),
    # T-SQL reads the backslash the same way, and has no wire-level backstop
    # to fall back on, so the gate has to hold on both engines.
    ("mssql", r"UPDATE dbo.t SET a = '\' -- ' WHERE id = 42"),
    ("mssql", r"UPDATE dbo.t SET a = N'\' -- ' WHERE id = 42"),
    ("mssql", r"UPDATE dbo.t SET a='\' WHERE id=1; EXEC xp_cmdshell 'whoami'; --'"),
]


@pytest.mark.parametrize("engine,sql", COUNT_BALANCED_PAYLOADS)
def test_a_single_statement_that_loses_its_where_is_blocked(engine, sql):
    report = qs.analyze(sql, engine=engine)
    assert report.blocked, f"count-balanced payload passed: {sql}"
    assert any("backslash before a quote" in b for b in report.blockers), \
        report.blockers


def test_the_count_check_alone_could_never_have_caught_it():
    """Pins WHY a second gate was needed rather than a wider count check: on
    this payload both parsers really do see one statement."""
    from queryhub import engines
    sql = r"UPDATE accounts SET balance = 0, note = '\' --' WHERE id = 42"
    assert qs._statement_count_disagrees(sql, engines.spec("postgres")) is False
    assert qs.analyze(sql).blocked          # ...and it is blocked anyway


@pytest.mark.parametrize("sql", [
    # The project's own false-positive corpus, re-run against the new gate.
    r"SELECT 'C:\path\to\file' AS p",
    r"SELECT regexp_replace(x, '\s+', ' ') FROM t",
    "SELECT 'O''Brien' AS name",
    # An E-string is where a backslash escape is REAL in PostgreSQL, so the
    # gate must not fire inside one.
    r"SELECT E'\'' AS quote",
    # Dollar quoting has no escape processing at all.
    "SELECT $body$ a \\' b $body$ AS raw",
    # A backslash inside a quoted IDENTIFIER is not a literal.
    r'SELECT "odd\" AS c FROM t',
    "UPDATE t SET a = 1 WHERE id = 1",
    r"SELECT * FROM t WHERE a LIKE '%\_%'",
])
def test_the_new_gate_keeps_the_false_positive_corpus_clean(sql):
    report = qs.analyze(sql)
    assert not report.blocked, f"false positive: {sql} -> {report.blockers}"


def test_the_escape_hint_is_engine_appropriate():
    """E'...' is a PostgreSQL extension. Telling a SQL Server user to use one
    sends them to a syntax error, so the advice is per engine."""
    pg = qs.analyze(r"UPDATE t SET a = '\' --' WHERE id = 1", engine="postgres")
    ms = qs.analyze(r"UPDATE dbo.t SET a = '\' --' WHERE id = 1", engine="mssql")
    assert "E'...'" in " ".join(pg.blockers)
    assert "E'...'" not in " ".join(ms.blockers)
    assert "''" in " ".join(ms.blockers)


# ---------------------------------------------------------------------------
# EXPLAIN ANALYSE — the British spelling. PostgreSQL accepts it as an exact
# synonym; the gate matched only the American one, so `EXPLAIN ANALYSE` walked
# past both the allow_explain_analyze switch AND the inner AST scan.
#
# Measured with the toggle off, 2026-07-30, before the fix:
#   blocked=True   EXPLAIN ANALYZE SELECT 1
#   blocked=False  EXPLAIN ANALYSE SELECT 1
#   blocked=False  EXPLAIN ANALYSE UPDATE t SET a=1        <- performs the write
#   blocked=False  EXPLAIN ANALYSE SELECT pg_read_file('/etc/passwd')
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "EXPLAIN ANALYZE SELECT 1",
    "EXPLAIN ANALYSE SELECT 1",
    "EXPLAIN (ANALYZE) SELECT 1",
    "EXPLAIN (ANALYSE) SELECT 1",
    "EXPLAIN (ANALYSE, BUFFERS) SELECT 1",
    "explain analyse select 1",
])
def test_both_spellings_respect_the_toggle_when_it_is_off(monkeypatch, sql):
    monkeypatch.setattr(qs, "_explain_analyze_allowed", lambda: False)
    report = qs.analyze(sql)
    assert report.blocked, f"{sql} ran with the toggle off"


@pytest.mark.parametrize("sql", [
    "EXPLAIN ANALYZE UPDATE t SET a = 1 WHERE id = 1",
    "EXPLAIN ANALYSE UPDATE t SET a = 1 WHERE id = 1",
    "EXPLAIN ANALYSE DELETE FROM t WHERE id = 1",
    "EXPLAIN ANALYSE DROP TABLE t",
])
def test_neither_spelling_may_execute_a_write_even_with_the_toggle_on(
        monkeypatch, sql):
    """The toggle allows EXPLAIN ANALYZE of a READ. ANALYZE really runs the
    wrapped statement, so a write stays blocked regardless."""
    monkeypatch.setattr(qs, "_explain_analyze_allowed", lambda: True)
    report = qs.analyze(sql)
    assert report.blocked, f"{sql} would have performed the write"


@pytest.mark.parametrize("sql", [
    "EXPLAIN ANALYZE SELECT 1",
    "EXPLAIN ANALYSE SELECT 1",
])
def test_both_spellings_are_allowed_for_reads_when_the_toggle_is_on(
        monkeypatch, sql):
    monkeypatch.setattr(qs, "_explain_analyze_allowed", lambda: True)
    assert not qs.analyze(sql).blocked


@pytest.mark.parametrize("sql", [
    "EXPLAIN ANALYSE SELECT pg_read_file('/etc/passwd')",
    "EXPLAIN ANALYZE SELECT pg_read_file('/etc/passwd')",
])
def test_the_inner_ast_scan_sees_through_both_spellings(monkeypatch, sql):
    """sqlglot parses the whole EXPLAIN as an opaque Command, so the inner
    statement is scanned separately. That scan keyed off the same regex."""
    monkeypatch.setattr(qs, "_explain_analyze_allowed", lambda: True)
    assert qs.analyze(sql).blocked


def test_plain_explain_is_untouched():
    """It never executes anything, so it must stay free even with the toggle
    off — this is the pre-flight path the product uses on every submit."""
    assert not qs.analyze("EXPLAIN SELECT 1").blocked
    assert not qs.analyze("EXPLAIN (VERBOSE) SELECT 1").blocked
