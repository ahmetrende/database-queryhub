"""The rate-limit and duplicate guards are re-checked
inside the create transaction (under a per-user advisory lock), so two
submissions that race validate_submission() can't both get inserted.

These exercise the re-check logic directly with a scripted cursor; the
advisory lock that makes it atomic is a plain pg_advisory_xact_lock call in
create_request().
"""
from queryhub import core_submit as cs


class _Cur:
    def __init__(self, results):
        self._results = list(results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._results.pop(0)


def _prep(uid="U0EXAMPLE01"):
    target = type("T", (), {"id": 7})()
    return cs.Prepared(
        user_id=uid, user_name="n", target=target, database="db",
        query="SELECT 1", required_mode="ro", justification=None,
        wants_result=True, result_format="csv", sched_for=None,
        explain_plan=None, risk_summary=None,
    )


def test_under_cap_no_duplicate_passes(monkeypatch):
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.cfg, "get_int", lambda k, d=None: 5)
    cur = _Cur([{"n": 2}, None])
    assert cs._recheck_open_limits(cur, _prep(), None) is None


def test_over_cap_is_rate_limited(monkeypatch):
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.cfg, "get_int", lambda k, d=None: 5)
    cur = _Cur([{"n": 5}])  # dup query never reached
    rej = cs._recheck_open_limits(cur, _prep(), None)
    assert isinstance(rej, cs.Rejection) and rej.field == "rate_limit"


def test_duplicate_is_blocked(monkeypatch):
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.cfg, "get_int", lambda k, d=None: 5)
    cur = _Cur([{"n": 1}, {"id": 9, "status": "pending"}])
    rej = cs._recheck_open_limits(cur, _prep(), None)
    assert isinstance(rej, cs.Rejection) and rej.reason == "duplicate"


def test_admin_skips_rate_limit_but_still_dedupes(monkeypatch):
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)
    # Only the duplicate query runs for an admin (no count query).
    cur = _Cur([{"id": 3, "status": "approved"}])
    rej = cs._recheck_open_limits(cur, _prep(), None)
    assert isinstance(rej, cs.Rejection) and rej.reason == "duplicate"
    assert len(cur.executed) == 1


def test_supersedes_id_is_excluded_from_both_checks(monkeypatch):
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.cfg, "get_int", lambda k, d=None: 5)
    cur = _Cur([{"n": 0}, None])
    assert cs._recheck_open_limits(cur, _prep(), supersedes_id=42) is None
    # Both statements carry the exclusion clause + the id parameter.
    for sql, params in cur.executed:
        assert "id <> %s" in sql
        assert params[-1] == 42
