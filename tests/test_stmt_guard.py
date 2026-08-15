"""The server is the only authority that cannot be wrong about a batch.

PostgreSQL refuses a second command at the wire because the executor uses the
extended protocol. SQL Server does not — measured on this fleet 2026-07-30,
a two-command batch runs unchallenged both through a plain `execute()` and
through a parameterized one:

    plain execute        RAN  first=[(1,)] nextset=True
    execute with params  RAN  first=[(1,)] nextset=True

So on SQL Server the guard asks the server to COMPILE the batch without
running it, and counts the statements it reports. Measured against the live
server the same day:

    SET SHOWPLAN_XML ON
      SELECT 1                       -> 1 statement
      SELECT 1; SELECT 2             -> 2
      SELECT 1; SELECT 2; SELECT 3   -> 3

These tests use a fake cursor rather than that server: the point being pinned
is the DECISION (block on disagreement, stay quiet when the server will not
answer), and a test that needs a SQL Server instance would not run in CI.
"""
import pytest

from queryhub import stmt_guard


def _plan(n_statements):
    """A SHOWPLAN document with `n` statement elements, namespaced the way SQL
    Server really emits it."""
    stmts = "".join(
        f'<StmtSimple StatementText="SELECT {i}" />' for i in range(n_statements))
    return ('<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/'
            f'showplan"><BatchSequence><Batch><Statements>{stmts}'
            "</Statements></Batch></BatchSequence></ShowPlanXML>")


class FakeCursor:
    """Records every statement issued, so the tests can assert SHOWPLAN was
    turned back off. `plan_rows` is what the counting execute() returns."""

    def __init__(self, n_statements=1, fail_on=None, plan_docs=None):
        self.executed = []
        self._n = n_statements
        self._fail_on = fail_on or set()
        self._plan_docs = plan_docs
        self._rows = []

    def execute(self, sql, *args, **kwargs):
        self.executed.append(sql)
        for frag in self._fail_on:
            if frag in sql:
                raise RuntimeError(f"denied: {frag}")
        if sql.startswith("SET SHOWPLAN"):
            self._rows = []
        else:
            docs = self._plan_docs
            if docs is None:
                docs = [_plan(self._n)]
            self._rows = [(d,) for d in docs]
        return self

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def nextset(self):
        return False


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

def test_one_statement_passes():
    cur = FakeCursor(n_statements=1)
    stmt_guard.check(cur, "SELECT 1", engine="mssql")      # no raise


def test_two_statements_are_refused():
    """The whole point: the approver reviewed one statement, the server found
    two, so nothing runs."""
    cur = FakeCursor(n_statements=2)
    with pytest.raises(stmt_guard.TooManyStatements) as e:
        stmt_guard.check(cur, "SELECT 1", engine="mssql")
    assert "2 separate statements" in str(e.value)
    assert "reviewed and approved as one" in str(e.value)


def test_the_error_names_a_way_forward():
    """A blocked user needs to know what to do instead, or they will assume the
    tool is broken and route around it."""
    cur = FakeCursor(n_statements=3)
    with pytest.raises(stmt_guard.TooManyStatements) as e:
        stmt_guard.check(cur, "SELECT 1", engine="mssql")
    assert "separately" in str(e.value) or "batch" in str(e.value)


# ---------------------------------------------------------------------------
# an unavailable check must not take the gateway down
# ---------------------------------------------------------------------------

def test_no_resolver_means_no_check():
    """Postgres is covered at the wire, so the guard must do nothing there —
    including issuing no statements at all on the cursor."""
    cur = FakeCursor(n_statements=99)
    stmt_guard.check(cur, "SELECT 1; SELECT 2", engine="postgres")
    assert cur.executed == []
    assert stmt_guard.supported("postgres") is False
    assert stmt_guard.supported("mssql") is True


def test_missing_showplan_permission_fails_open():
    """SHOWPLAN needs a permission the login may not hold on every database.
    Losing the check puts us back where we were, which is survivable; refusing
    to run every query on that database is not."""
    cur = FakeCursor(fail_on={"SET SHOWPLAN_XML ON"})
    stmt_guard.check(cur, "SELECT 1", engine="mssql")      # no raise


def test_a_statement_the_optimizer_cannot_compile_fails_open():
    """A reference to a temp table this session has not created yet cannot be
    compiled, and that is not evidence of smuggling."""
    cur = FakeCursor(fail_on={"SELECT * FROM #tmp"})
    stmt_guard.check(cur, "SELECT * FROM #tmp", engine="mssql")


def test_an_unparseable_plan_document_fails_open():
    cur = FakeCursor(plan_docs=["<not-xml"])
    stmt_guard.check(cur, "SELECT 1", engine="mssql")


def test_an_empty_plan_is_not_read_as_zero_statements():
    """`n or None` — a plan with no statement elements means the server did not
    tell us, not that the batch is empty."""
    cur = FakeCursor(plan_docs=[_plan(0)])
    stmt_guard.check(cur, "SELECT 1", engine="mssql")
    assert stmt_guard._mssql_statement_count(FakeCursor(plan_docs=[_plan(0)]),
                                             "SELECT 1") is None


# ---------------------------------------------------------------------------
# plan mode must never leak onto the real statement
# ---------------------------------------------------------------------------

def test_showplan_is_turned_off_on_the_happy_path():
    """Left on, the caller's real statement would return a plan document
    instead of rows — the guard would silently break every result."""
    cur = FakeCursor(n_statements=1)
    stmt_guard.check(cur, "SELECT 1", engine="mssql")
    assert cur.executed[0] == "SET SHOWPLAN_XML ON"
    assert cur.executed[-1] == "SET SHOWPLAN_XML OFF"


def test_showplan_is_turned_off_when_the_count_refuses():
    cur = FakeCursor(n_statements=2)
    with pytest.raises(stmt_guard.TooManyStatements):
        stmt_guard.check(cur, "SELECT 1", engine="mssql")
    assert "SET SHOWPLAN_XML OFF" in cur.executed


def test_showplan_is_turned_off_when_the_compile_fails():
    cur = FakeCursor(fail_on={"SELECT * FROM #tmp"})
    stmt_guard.check(cur, "SELECT * FROM #tmp", engine="mssql")
    assert cur.executed[-1] == "SET SHOWPLAN_XML OFF"


def test_a_cursor_stuck_in_plan_mode_raises_rather_than_returning_rows():
    """If SHOWPLAN cannot be turned off the cursor is unusable, and returning
    quietly would hand the caller plan XML in place of their result set."""
    cur = FakeCursor(fail_on={"SET SHOWPLAN_XML OFF"})
    with pytest.raises(RuntimeError):
        stmt_guard.check(cur, "SELECT 1", engine="mssql")


# ---------------------------------------------------------------------------
# the wiring
# ---------------------------------------------------------------------------

def test_the_executor_checks_before_it_opens_a_portal():
    """Issuing a statement while a portal is open blocks forever with no
    client-side timeout — the 2026-07-30 outage. A guard placed after the
    execute would hang instead of protecting anything."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src"
           / "queryhub" / "executor.py").read_text(encoding="utf-8")
    i_guard = src.index("stmt_guard.check(cur, stmt.rewritten")
    i_stream = src.index("stream = cur.stream(stmt.rewritten)")
    i_exec = src.index("cur.execute(stmt.rewritten)")
    assert i_guard < i_stream and i_guard < i_exec


def test_the_counted_text_is_the_text_that_runs():
    """Counting `stmt.sql` while executing `stmt.rewritten` would verify the
    wrong string."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src"
           / "queryhub" / "executor.py").read_text(encoding="utf-8")
    assert "stmt_guard.check(cur, stmt.rewritten, engine=engine" in src
