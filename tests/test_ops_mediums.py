"""Operational fixes: drain, offboarding, config invariants, worker concurrency.

- `submit()` ignored drain, so an approval or scheduler dispatch landing during
  a restart started a query the shutdown was about to interrupt.
- Disabling a `requesters` row is the documented leaver step, but it only gated
  /sql entry: a request already approved (or scheduled for next week) still ran
  after the person left, because their grant row was untouched.
- `execution_lease_sec` and `query_timeout_sec` were edited independently, so
  the lease could drop below the timeout and the orphan reconciler would fail
  queries that were still running.
- A global lock wrapped the whole CSV write, serialising every result-producing
  query: 4 configured workers, effective concurrency 1.
"""
import pytest

from queryhub import executor as ex
from queryhub import lifecycle
from queryhub.web import config_admin as ca


class _FakeFuture:
    """submit() reads the Future it gets back (to surface an exception the
    worker would otherwise swallow), so a stub has to return one."""

    def exception(self):
        return None

    def add_done_callback(self, fn):
        fn(self)


def test_submit_refuses_new_work_while_draining(monkeypatch):
    submitted = []
    monkeypatch.setattr(ex._pool, "submit",
                        lambda fn, *a, **k: submitted.append(a) or _FakeFuture())
    monkeypatch.setattr(lifecycle, "is_draining", lambda: True)
    ex.submit({"id": 7}, client=None)
    assert submitted == [], "started new work during drain"


def test_submit_runs_normally_when_not_draining(monkeypatch):
    submitted = []
    monkeypatch.setattr(ex._pool, "submit",
                        lambda fn, *a, **k: submitted.append(a) or _FakeFuture())
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    ex.submit({"id": 7}, client=None)
    assert len(submitted) == 1


def _wire_run(monkeypatch, *, allowed: bool, is_admin: bool = False):
    target = type("T", (), {"engine": "postgres", "alias": "svc", "id": 7,
                            "default_database": "app"})()
    monkeypatch.setattr(ex.targets, "get", lambda tid: target)
    monkeypatch.setattr(ex.engines, "is_executable", lambda e: True)
    report = type("R", (), {"blocked": False, "blockers": [],
                            "main_tier": "ro", "statements": [1]})()
    monkeypatch.setattr(ex.query_safety, "analyze", lambda *a, **k: report)
    monkeypatch.setattr(ex.admins, "is_admin", lambda uid: is_admin)
    monkeypatch.setattr(ex.requesters, "is_allowed", lambda uid: allowed)
    monkeypatch.setattr(ex.teams, "effective_mode_for_database",
                        lambda *a, **k: "ddl")
    seen = {}
    monkeypatch.setattr(ex, "_fail", lambda c, r, m: seen.setdefault("fail", m))

    # `seen.setdefault("creds", True) or ("u", "p")` used to be inlined here and
    # was wrong: setdefault returns True, so `or` short-circuited and the
    # executor received a bool where it unpacks a (user, password) pair. The
    # resulting TypeError was swallowed by the executor's broad handler, so the
    # test passed while proving nothing about credentials.
    def _creds(*a, **k):
        seen["creds"] = True
        return ("u", "p")
    monkeypatch.setattr(ex.targets, "get_credentials", _creds)
    monkeypatch.setattr(ex.row_limits, "effective_caps", lambda uid: (10, 10))
    # The post-failure terminal-state check reads `requests.status`, and an
    # authorized run goes on to the exclusive claim UPDATE and the team-role
    # lookup. Unpatched, any of the three opens the real connection pool.
    monkeypatch.setattr(ex.db, "fetch_one", lambda *a, **k: None)

    class _Cur:
        rowcount = 1
        def execute(self, *a, **k): pass
        def fetchone(self): return None

    class _Txn:
        def __enter__(self): return _Cur()
        def __exit__(self, *a): return False

    monkeypatch.setattr(ex.db, "transaction", lambda: _Txn())
    # Stop before a real query connection: the claim is as far as these tests go.
    monkeypatch.setattr(ex, "_build_application_name",
                        lambda req: seen.setdefault("claimed", True) and "app")
    return seen


def test_withdrawn_requester_cannot_execute_an_approved_request(monkeypatch):
    seen = _wire_run(monkeypatch, allowed=False)
    ex._run({"id": 1, "target_server_id": 7, "database_name": "app",
             "query": "SELECT 1", "requester_slack_id": "U0GONE"}, client=None)
    assert "creds" not in seen, "ran a query for a de-provisioned requester"
    assert "withdrawn" in seen.get("fail", "")


def test_admin_requester_still_passes_the_whitelist_check(monkeypatch):
    seen = _wire_run(monkeypatch, allowed=False, is_admin=True)
    ex._run({"id": 1, "target_server_id": 7, "database_name": "app",
             "query": "SELECT 1", "requester_slack_id": "U0ADMIN"}, client=None)
    assert "fail" not in seen or "withdrawn" not in seen.get("fail", "")


@pytest.mark.parametrize("lease,timeout,ok", [
    (900, 300, True),
    (400, 300, True),
    (330, 300, True),
    (310, 300, False),   # no margin for result streaming
    (300, 300, False),   # equal: reconciler races the query
    (120, 300, False),   # lease below timeout
])
def test_lease_must_exceed_query_timeout(lease, timeout, ok):
    problem = ca._lease_covers_timeout({"execution_lease_sec": str(lease),
                                        "query_timeout_sec": str(timeout)})
    assert (problem is None) is ok, problem


def test_streamable_leading_covers_reads_only():
    # DML must not stream: the server reports no rowcount for a streamed
    # statement, and the DML paths depend on it.
    assert "SELECT" in ex._STREAMABLE_LEADING
    assert "WITH" in ex._STREAMABLE_LEADING
    assert "UPDATE" not in ex._STREAMABLE_LEADING
    assert "DELETE" not in ex._STREAMABLE_LEADING
    assert "INSERT" not in ex._STREAMABLE_LEADING


def test_no_global_write_lock_remains():
    # The lock made 4 workers behave like 1 for exactly the slowest queries.
    assert not hasattr(ex, "_lock")


def test_results_dir_is_configurable(monkeypatch):
    """It was a hardcoded absolute path, which made the app unrunnable anywhere
    that path did not exist — a container being the obvious case."""
    monkeypatch.setenv("QH_RESULTS_DIR", "/tmp/qh-results-test")
    assert str(ex._results_dir()) == "/tmp/qh-results-test"
    monkeypatch.delenv("QH_RESULTS_DIR")
    # The default is unchanged, so an existing install keeps finding its files.
    assert str(ex._results_dir()) == "/var/lib/queryhub/results"
    monkeypatch.setenv("QH_RESULTS_DIR", "   ")
    assert str(ex._results_dir()) == "/var/lib/queryhub/results"


def test_an_exception_escaping_the_worker_is_logged(monkeypatch, caplog):
    """The silent failure this closes: a ThreadPoolExecutor keeps an escaped
    exception in the Future, and nobody was reading it — so an unwritable
    results directory left the request in 'executing' with nothing in the log
    until the lease reconciler timed it out.
    """
    import logging

    class _Boom:
        def exception(self):
            return PermissionError("[Errno 13] Permission denied: "
                                   "'/var/lib/queryhub/results'")

        def add_done_callback(self, fn):
            fn(self)

    monkeypatch.setattr(ex._pool, "submit", lambda fn, *a, **k: _Boom())
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)

    with caplog.at_level(logging.ERROR, logger="queryhub.executor"):
        ex.submit({"id": 99}, client=None)

    assert any("unhandled exception" in r.message for r in caplog.records), \
        "an exception that escaped the worker was swallowed"
    assert any("Permission denied" in (r.exc_text or "")
               for r in caplog.records if r.exc_text), \
        "the traceback was not attached"
