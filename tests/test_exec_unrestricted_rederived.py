"""The executor decides `unrestricted` itself, from who the requester is NOW.

Two failures this prevents, and they pull in opposite directions — which is why
the answer cannot be a flag stored on the request row:

  * CORRECTNESS. A super-admin confirms a WHERE-less UPDATE at submit. The
    executor re-analyses the stored query before running it (so an edit made
    after approval cannot slip through). Analysing it *restricted* would block
    the statement at execution, after the operator already answered for it —
    the confirmation would have meant nothing. This was the real state of the
    code until the re-derivation was added; nothing else caught it, because
    every test up to then submitted queries that were legal for everyone.

  * AUTHORIZATION. The mirror case: a request submitted while the requester was
    a super-admin must NOT keep that standing once the row is gone. Same shape
    as the tier re-check next to it, which refuses a grant revoked after
    approval.

The real analyzer runs here on purpose. Stubbing it would test the plumbing and
miss the thing that matters: which verdict the statement actually gets.
"""
from __future__ import annotations

import pytest

from dba_slack_bot import executor as ex

WHERELESS = "UPDATE rewards SET flag = 1"


def _target():
    return type("T", (), {"enabled": True, "engine": "postgres", "alias": "svc",
                          "id": 7, "default_database": "app"})()


@pytest.fixture
def run(monkeypatch):
    """Drive _run as far as the credential fetch, with the REAL safety pass."""
    state = {"super": False}
    seen: dict = {}
    monkeypatch.setattr(ex.targets, "get", lambda tid: _target())
    monkeypatch.setattr(ex.engines, "is_executable", lambda e: True)
    monkeypatch.setattr(ex.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(ex.admins, "is_super_admin", lambda uid: state["super"])
    monkeypatch.setattr(ex.requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(ex.teams, "effective_mode_for_database",
                        lambda *a, **k: "ddl")
    monkeypatch.setattr(ex, "_fail",
                        lambda c, r, m: seen.setdefault("fail", m))

    def _creds(*a, **k):
        seen["reached_credentials"] = True
        raise LookupError("stop here — the gate is behind us")

    monkeypatch.setattr(ex.targets, "get_credentials", _creds)

    def go(super_admin: bool, query: str = WHERELESS):
        state["super"] = super_admin
        seen.clear()
        request = {
            "id": 1, "target_server_id": 7, "database_name": "app",
            "query": query, "requester_slack_id": "U0EXAMPLE01",
            "wants_result": True, "result_format": "csv", "engine": "postgres",
            "required_tier": "rw", "bundle_id": None, "origin": "web",
        }
        try:
            ex._run(request, None)
        except LookupError:
            pass
        return seen

    return go


def test_a_super_admins_whereless_update_is_not_blocked_at_execution(run):
    # `reached_credentials` is the signal, not the absence of a failure: the
    # stub raises LookupError to stop the run, and the executor turns that into
    # its own "credentials not configured" message. Getting there at all means
    # the safety gate let the statement through.
    seen = run(super_admin=True)
    assert seen.get("reached_credentials"), (
        f"blocked at execution after the operator already confirmed it: "
        f"{seen.get('fail')}")
    assert "WHERE" not in (seen.get("fail") or "")


def test_the_same_statement_is_blocked_for_everyone_else(run):
    seen = run(super_admin=False)
    assert "fail" in seen, "a WHERE-less UPDATE ran for a non-super-admin"
    assert "WHERE" in seen["fail"]
    assert "reached_credentials" not in seen


def test_losing_super_admin_after_approval_blocks_the_queued_statement(run):
    """The authorization direction: approved as a super-admin, demoted before
    it ran. The row still says approved; the standing is gone."""
    assert run(super_admin=True).get("reached_credentials")   # would have run
    seen = run(super_admin=False)                             # row gone meanwhile
    assert "fail" in seen and "WHERE" in seen["fail"]
    assert "reached_credentials" not in seen


def test_an_ordinary_statement_is_unaffected_either_way(run):
    for who in (True, False):
        seen = run(super_admin=who, query="UPDATE rewards SET flag = 1 WHERE id = 5")
        assert seen.get("reached_credentials"), seen.get("fail")


def test_the_audit_killer_is_blocked_at_execution_for_a_super_admin(run):
    """Whatever happened at submit, this never runs."""
    seen = run(super_admin=True,
               query="ALTER DATABASE app SET log_statement = 'none'")
    assert "fail" in seen
    assert "logging" in seen["fail"]
    assert "reached_credentials" not in seen
