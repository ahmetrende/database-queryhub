"""SQL Server writes commit whole or not at all, and Stop reaches the cursor.

Two findings, pinned with fake connections and cursors. The live behaviour is
in tests/test_integration_mssql.py, which needs a real SQL Server.

- A write needs a statement count. Without one, the run stops and rolls back.
  The run also stops when SQL Server ends the open transaction between two
  statements. Before, the script committed only part of its work.
- Stop cancels the pyodbc cursor. Before, Stop reported FAILED, because SQL
  Server gives no backend pid.
"""
import threading
import time

import pytest

from queryhub import cancellation, executor, mssql_exec, query_safety, stmt_guard


# --- fakes ------------------------------------------------------------------


class _Cur:
    def __init__(self):
        self.cancels = 0

    def cancel(self):
        self.cancels += 1


class _Conn:
    def __init__(self):
        self.cur = _Cur()
        self.calls = []

    def cursor(self):
        return self.cur

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")


class _Result:
    csv_path = None
    has_result_set = False
    rowcount = 1


class _GuardCursor:
    """The SHOWPLAN conversation of stmt_guard. `fail_on` makes one statement
    raise, the way a refused SHOWPLAN does."""

    def __init__(self, fail_on=None):
        self.executed = []
        self._fail_on = fail_on
        self._rows = []

    def execute(self, sql, *args):
        self.executed.append(sql)
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError("SHOWPLAN permission denied")
        self._rows = [] if sql.startswith("SET SHOWPLAN") else [(
            '<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/'
            'showplan"><BatchSequence><Batch><Statements><StmtSimple />'
            "</Statements></Batch></BatchSequence></ShowPlanXML>",)]
        return self

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def nextset(self):
        return False


TARGET = type("T", (), {"id": 5, "alias": "legacy-sql", "engine": "mssql",
                        "host": "listener.example.test", "port": 1433})()
REQUEST = {"id": 77, "database_name": "app", "wants_result": False,
           "requester_slack_id": "U0EXAMPLE001"}


def _report(*sqls, tier="rw"):
    return query_safety.SafetyReport(main_tier=tier, statements=[
        query_safety.StatementInfo(raw=s, rewritten=s, kind=tier,
                                   leading=s.split()[0].upper())
        for s in sqls])


@pytest.fixture
def run_env(monkeypatch):
    """`_run_mssql` with a fake connection. `statement` replaces the shared
    statement runner. It gets (index, kwargs)."""
    env = {"conn": _Conn(), "finalized": False, "seen": []}

    def statement(index, kw):
        return _Result()
    env["statement"] = statement

    def fake_main(cur, s, i, *a, **k):
        env["seen"].append(dict(k))
        return env["statement"](i, k)

    monkeypatch.setattr(mssql_exec, "connect", lambda *a, **k: env["conn"])
    monkeypatch.setattr(mssql_exec, "describe_columns", lambda *a: None)
    # An RO run asks the bot DB for a readable secondary: none here, use the primary.
    monkeypatch.setattr(mssql_exec, "resolve_ro_endpoint", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_execute_main_statement", fake_main)
    monkeypatch.setattr(executor, "_more_mssql_results", lambda *a, **k: [])
    monkeypatch.setattr(executor, "_finalize",
                        lambda *a, **k: env.__setitem__("finalized", True))
    return env


def _run(report, mode="rw"):
    executor._run_mssql(None, dict(REQUEST), TARGET, mode, report, "u", "p",
                        30, 100, 10 ** 6)


# --- B01: the statement count is required for a write ------------------------


def test_require_count_refuses_when_the_server_gives_no_count():
    cur = _GuardCursor(fail_on="INSERT")
    with pytest.raises(stmt_guard.CountUnavailable) as caught:
        stmt_guard.check(cur, "INSERT INTO t VALUES (1)", engine="mssql",
                         require_count=True)
    assert "SHOWPLAN" in str(caught.value)
    assert "Nothing was changed" in str(caught.value)
    assert cur.executed[-1] == "SET SHOWPLAN_XML OFF"


def test_require_count_passes_one_counted_statement():
    stmt_guard.check(_GuardCursor(), "INSERT INTO t VALUES (1)", engine="mssql",
                     require_count=True)                    # no raise


def test_a_read_without_a_count_still_runs():
    stmt_guard.check(_GuardCursor(fail_on="SELECT"), "SELECT * FROM t",
                     engine="mssql")                        # no raise


def test_require_count_does_nothing_on_postgres():
    cur = _GuardCursor()
    stmt_guard.check(cur, "INSERT INTO t VALUES (1)", engine="postgres",
                     require_count=True)
    assert cur.executed == []


def test_a_write_asks_for_the_count_and_a_read_does_not(run_env, monkeypatch):
    monkeypatch.setattr(mssql_exec, "open_transactions", lambda cur: 1)
    _run(_report("INSERT INTO t VALUES (1)"), mode="rw")
    assert run_env["seen"][0]["require_count"] is True
    assert callable(run_env["seen"][0]["before_run"])

    run_env["seen"].clear()
    _run(_report("SELECT 1", tier="ro"), mode="ro")
    assert "require_count" not in run_env["seen"][0]
    assert "before_run" not in run_env["seen"][0]


def test_a_missing_count_on_the_second_statement_rolls_back_the_script(
        run_env, monkeypatch):
    monkeypatch.setattr(mssql_exec, "open_transactions", lambda cur: 1)

    def statement(index, kw):
        if index == 2:
            raise stmt_guard.CountUnavailable(stmt_guard.COUNT_UNAVAILABLE_MESSAGE)
        return _Result()
    run_env["statement"] = statement
    with pytest.raises(stmt_guard.CountUnavailable):
        _run(_report("INSERT INTO t VALUES (1)", "UPDATE t SET a = 2"))
    assert "commit" not in run_env["conn"].calls
    assert run_env["conn"].calls[:1] == ["rollback"]
    assert run_env["finalized"] is False


def test_a_lost_transaction_rolls_back_and_does_not_commit(run_env, monkeypatch):
    """After statement 1 the session had a transaction. Before statement 2
    runs, it has none: the work of statement 1 is gone."""
    counts = iter([1, 0])
    monkeypatch.setattr(mssql_exec, "open_transactions", lambda cur: next(counts))

    def statement(index, kw):
        kw["before_run"]()
        return _Result()
    run_env["statement"] = statement
    with pytest.raises(executor.MssqlTransactionLost) as caught:
        _run(_report("INSERT INTO t VALUES (1)", "UPDATE t SET a = 2"))
    assert "before statement 2" in str(caught.value)
    assert "commit" not in run_env["conn"].calls
    assert "rollback" in run_env["conn"].calls


def test_a_kept_transaction_commits(run_env, monkeypatch):
    monkeypatch.setattr(mssql_exec, "open_transactions", lambda cur: 1)

    def statement(index, kw):
        kw["before_run"]()
        return _Result()
    run_env["statement"] = statement
    _run(_report("INSERT INTO t VALUES (1)", "UPDATE t SET a = 2"))
    assert run_env["conn"].calls == ["commit", "close"]
    assert run_env["finalized"] is True


def test_no_transaction_before_the_first_statement_is_not_a_loss(
        run_env, monkeypatch):
    """A statement that opens no transaction (SELECT 1, for instance) leaves
    nothing to lose. Only a transaction that was open and is gone stops it."""
    monkeypatch.setattr(mssql_exec, "open_transactions", lambda cur: 0)

    def statement(index, kw):
        kw["before_run"]()
        return _Result()
    run_env["statement"] = statement
    _run(_report("DECLARE @a int", "UPDATE t SET a = 2"))
    assert "commit" in run_env["conn"].calls


# --- B16: Stop reaches the cursor ---------------------------------------------


def test_cancel_running_finds_the_cursor_only_while_it_runs():
    cur = _Cur()
    assert mssql_exec.cancel_running(91) == "absent"
    with mssql_exec.RunningRequest(91, cur) as run:
        assert mssql_exec.cancel_running(91) == "cancelled"
        assert mssql_exec.cancel_running(91) == "cancelled"
        assert run.cancelled and cur.cancels == 1
        assert run.begin_commit() is False
    assert mssql_exec.cancel_running(91) == "absent"


def test_a_stop_after_the_commit_starts_does_nothing():
    cur = _Cur()
    with mssql_exec.RunningRequest(92, cur) as run:
        assert run.begin_commit() is True
        assert mssql_exec.cancel_running(92) == "too_late"
    assert cur.cancels == 0


def test_the_watch_thread_cancels_on_a_stop_from_another_process():
    cur, asked = _Cur(), threading.Event()
    with mssql_exec.RunningRequest(93, cur, is_cancelled=asked.is_set,
                                   poll_sec=0.01) as run:
        asked.set()
        deadline = time.monotonic() + 5
        while not run.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        assert run.cancelled and cur.cancels == 1


def test_a_failing_stop_poll_does_not_end_the_watch():
    cur, calls = _Cur(), []

    def is_cancelled():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bot DB blip")
        return True
    with mssql_exec.RunningRequest(94, cur, is_cancelled=is_cancelled,
                                   poll_sec=0.01) as run:
        deadline = time.monotonic() + 5
        while not run.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        assert run.cancelled


def test_a_stop_during_a_statement_rolls_back_and_raises_stopped(run_env):
    def statement(index, kw):
        assert mssql_exec.cancel_running(REQUEST["id"]) == "cancelled"
        raise RuntimeError("HY008 Operation canceled")
    run_env["statement"] = statement
    with pytest.raises(executor.MssqlStopped):
        _run(_report("UPDATE t SET a = 2"))
    assert run_env["conn"].cur.cancels == 1
    assert "commit" not in run_env["conn"].calls
    assert "rollback" in run_env["conn"].calls


def test_a_stop_between_statements_skips_the_rest(run_env, monkeypatch):
    monkeypatch.setattr(mssql_exec, "open_transactions", lambda cur: 1)
    ran = []

    def statement(index, kw):
        ran.append(index)
        mssql_exec.cancel_running(REQUEST["id"])
        return _Result()
    run_env["statement"] = statement
    with pytest.raises(executor.MssqlStopped):
        _run(_report("INSERT INTO t VALUES (1)", "UPDATE t SET a = 2"))
    assert ran == [1]
    assert "commit" not in run_env["conn"].calls


def _stop_backend_env(monkeypatch):
    monkeypatch.setattr(cancellation.db, "fetch_one", lambda *a, **k: {
        "backend_pid": None, "engine_execution_id": None,
        "target_server_id": 5, "executed_target_id": None,
        "executed_tier": "rw", "database_name": "app", "st": "executing"})
    monkeypatch.setattr(cancellation.targets, "get", lambda _id: TARGET)


def test_stop_backend_cancels_a_sql_server_query_in_this_process(monkeypatch):
    _stop_backend_env(monkeypatch)
    cur = _Cur()
    with mssql_exec.RunningRequest(REQUEST["id"], cur):
        assert (cancellation.stop_backend(REQUEST["id"])
                == cancellation.CancelOutcome.CANCELLED)
    assert cur.cancels == 1


def test_stop_backend_signals_a_sql_server_query_in_another_process(monkeypatch):
    _stop_backend_env(monkeypatch)
    assert (cancellation.stop_backend(REQUEST["id"])
            == cancellation.CancelOutcome.SIGNALLED)


def test_stop_backend_after_the_commit_starts_reports_not_running(monkeypatch):
    _stop_backend_env(monkeypatch)
    with mssql_exec.RunningRequest(REQUEST["id"], _Cur()) as run:
        run.begin_commit()
        assert (cancellation.stop_backend(REQUEST["id"])
                == cancellation.CancelOutcome.NOT_RUNNING)


def test_a_signalled_stop_leaves_the_row_to_the_running_process(monkeypatch):
    """The process that runs the query fails the row after its rollback. If
    this process failed it first, a write could look stopped and still
    commit."""
    failed = []
    monkeypatch.setattr(cancellation, "request_cancel", lambda *a: None)
    monkeypatch.setattr(cancellation, "stop_backend",
                        lambda _id: cancellation.CancelOutcome.SIGNALLED)
    monkeypatch.setattr(cancellation, "_fail_request",
                        lambda *a: failed.append(a) or True)
    assert (cancellation.cancel(REQUEST["id"], "U0EXAMPLE001", "a user")
            == cancellation.CancelOutcome.SIGNALLED)
    assert failed == []


def test_the_web_stop_route_has_a_message_for_every_outcome():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src"
           / "queryhub" / "web" / "routes_queries.py").read_text(encoding="utf-8")
    for name in ("CANCELLED", "TERMINATED", "NOT_RUNNING", "FAILED", "SIGNALLED"):
        assert f"cancellation.CancelOutcome.{name}:" in src, name
