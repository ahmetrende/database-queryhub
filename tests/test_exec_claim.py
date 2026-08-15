"""Durable, idempotent execution.

- _run atomically claims the request (status -> executing) and aborts
  silently if the claim is lost, so a request cancelled or superseded
  between submission and execution never runs.
- resubmit_approved_on_boot re-queues requests orphaned in 'approved' by a
  hard crash (the in-memory worker queue does not survive an ungraceful
  stop).
"""
from queryhub import executor as ex


class _FakeCur:
    def __init__(self, rowcount):
        self.rowcount = rowcount
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)


class _FakeTxn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


def _target():
    return type("T", (), {
        "enabled": True, "engine": "postgres", "alias": "svc", "id": 7, "default_database": "app",
    })()


def _req():
    return {
        "id": 1, "target_server_id": 7, "database_name": "app",
        "query": "UPDATE t SET x = 1", "requester_slack_id": "U0EXAMPLE01",
    }


def test_lost_claim_aborts_without_executing(monkeypatch):
    monkeypatch.setattr(ex.targets, "get", lambda tid: _target())
    monkeypatch.setattr(ex.engines, "is_executable", lambda e: True)
    report = type("R", (), {
        "blocked": False, "blockers": [], "main_tier": "rw", "statements": [1],
    })()
    monkeypatch.setattr(ex.query_safety, "analyze", lambda *a, **k: report)
    monkeypatch.setattr(ex.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(ex.requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(ex.teams, "effective_mode_for_database", lambda *a, **k: "rw")
    monkeypatch.setattr(ex.targets, "get_credentials", lambda *a, **k: ("u", "pw"))
    monkeypatch.setattr(ex.row_limits, "effective_caps", lambda uid: (100, 1000))
    # The claim UPDATE affects 0 rows — the request was cancelled/superseded.
    # Terminal-state re-read after a failure; unpatched it dials the real DB.
    monkeypatch.setattr(ex.db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(ex.db, "transaction", lambda: _FakeTxn(_FakeCur(0)))

    reached = {}
    monkeypatch.setattr(ex, "_build_application_name",
                        lambda req: reached.setdefault("ran", True) or "app")
    monkeypatch.setattr(ex, "_fail", lambda c, r, m: reached.setdefault("fail", m))

    ex._run(_req(), client=None)
    assert "ran" not in reached, "execution proceeded despite a lost claim"
    # Silent abort — the terminal-state owner already messaged the user.
    assert "fail" not in reached


def test_won_claim_proceeds(monkeypatch):
    monkeypatch.setattr(ex.targets, "get", lambda tid: _target())
    monkeypatch.setattr(ex.engines, "is_executable", lambda e: True)
    report = type("R", (), {
        "blocked": False, "blockers": [], "main_tier": "ro", "statements": [1],
    })()
    monkeypatch.setattr(ex.query_safety, "analyze", lambda *a, **k: report)
    monkeypatch.setattr(ex.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(ex.requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(ex.teams, "effective_mode_for_database", lambda *a, **k: "ro")
    monkeypatch.setattr(ex.targets, "get_credentials", lambda *a, **k: ("u", "pw"))
    monkeypatch.setattr(ex.row_limits, "effective_caps", lambda uid: (100, 1000))
    monkeypatch.setattr(ex.audit, "log_in", lambda *a, **k: None)
    # Terminal-state re-read after a failure; unpatched it dials the real DB.
    monkeypatch.setattr(ex.db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(ex.db, "transaction", lambda: _FakeTxn(_FakeCur(1)))

    reached = {}
    # Stop right after the claim so the test doesn't open a real connection.
    def _boom(req):
        reached["ran"] = True
        raise RuntimeError("stop after claim")
    monkeypatch.setattr(ex, "_build_application_name", _boom)
    monkeypatch.setattr(ex, "_fail", lambda c, r, m: reached.setdefault("fail", m))

    ex._run(_req(), client=None)
    assert reached.get("ran") is True, "a won claim should proceed to execution"


def test_second_submission_of_the_same_request_is_refused(monkeypatch):
    """Exclusivity: the claim is 'approved' -> 'executing' and nothing else.

    The regression this guards: the claim used to accept status='executing'
    too, so a request sitting 'approved' in one process's worker queue could be
    picked up by another process's boot recovery and BOTH would claim it —
    running an RW/DDL statement twice. Here the first claim wins (1 row) and
    the second sees the row already 'executing' (0 rows) and must abort.
    """
    monkeypatch.setattr(ex.targets, "get", lambda tid: _target())
    monkeypatch.setattr(ex.engines, "is_executable", lambda e: True)
    report = type("R", (), {
        "blocked": False, "blockers": [], "main_tier": "rw", "statements": [1],
    })()
    monkeypatch.setattr(ex.query_safety, "analyze", lambda *a, **k: report)
    monkeypatch.setattr(ex.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(ex.requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(ex.teams, "effective_mode_for_database", lambda *a, **k: "rw")
    monkeypatch.setattr(ex.targets, "get_credentials", lambda *a, **k: ("u", "pw"))
    monkeypatch.setattr(ex.row_limits, "effective_caps", lambda uid: (100, 1000))
    monkeypatch.setattr(ex.audit, "log_in", lambda *a, **k: None)

    # A row that can be claimed exactly once, like the real UPDATE ... WHERE
    # status = 'approved'.
    state = {"status": "approved"}

    class _ClaimCur:
        rowcount = 0

        def execute(self, sql, params=None):
            if "SET status = 'executing'" in sql:
                if state["status"] == "approved":
                    state["status"] = "executing"
                    type(self).rowcount = 1
                else:
                    type(self).rowcount = 0

    class _Txn:
        def __enter__(self): return _ClaimCur()
        def __exit__(self, *a): return False

    monkeypatch.setattr(ex.db, "transaction", lambda: _Txn())
    # _team_role_for reads team_target_grants; unpatched it dials the real DB.
    monkeypatch.setattr(ex.db, "fetch_one", lambda *a, **k: None)
    runs = []
    monkeypatch.setattr(ex, "_build_application_name",
                        lambda req: runs.append(req["id"]) or "app")
    monkeypatch.setattr(ex, "_fail", lambda c, r, m: None)

    ex._run(_req(), client=None)   # first submission: wins the claim
    ex._run(_req(), client=None)   # duplicate submission: must be refused
    assert len(runs) == 1, f"request executed {len(runs)} times, expected once"


def test_resubmit_approved_on_boot_requeues_all(monkeypatch):
    rows = [{"id": 1}, {"id": 2}, {"id": 3}]
    monkeypatch.setattr(ex.db, "fetch_all", lambda *a, **k: rows)
    submitted = []
    monkeypatch.setattr(ex, "submit", lambda row, client: submitted.append(row["id"]))
    n = ex.resubmit_approved_on_boot(client=None)
    assert n == 3
    assert submitted == [1, 2, 3]
