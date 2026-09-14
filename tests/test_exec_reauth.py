"""The executor re-authorizes at execution time.

A request approved while the user held a grant can execute much later
(scheduled / bundled / queued). If the grant is revoked or downgraded in
the meantime, the executor must fail the request closed instead of running
it with a tier the user no longer holds.
"""
from dba_slack_bot import executor as ex


def _fake_target():
    return type("T", (), {
        "enabled": True, "engine": "postgres", "alias": "svc", "id": 7, "default_database": "app",
    })()


def _wire(monkeypatch, current_mode, main_tier="rw"):
    """Drive _run up to the re-auth gate with everything else stubbed."""
    monkeypatch.setattr(ex.targets, "get", lambda tid: _fake_target())
    monkeypatch.setattr(ex.engines, "is_executable", lambda e: True)
    report = type("R", (), {
        "blocked": False, "blockers": [], "main_tier": main_tier, "statements": [1],
    })()
    monkeypatch.setattr(ex.query_safety, "analyze", lambda *a, **k: report)
    monkeypatch.setattr(ex.admins, "is_admin", lambda uid: False)
    # The execution-time re-analysis re-derives `unrestricted` from the
    # requester's CURRENT super-admin standing, so the executor asks too.
    monkeypatch.setattr(ex.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(ex.requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(ex.teams, "effective_mode_for_database",
                        lambda *a, **k: current_mode)
    seen = {}
    monkeypatch.setattr(ex, "_fail", lambda c, r, m: seen.setdefault("fail", m))

    def _creds(*a, **k):
        seen["creds"] = True
        raise LookupError("stop here — past the re-auth gate")
    monkeypatch.setattr(ex.targets, "get_credentials", _creds)
    return seen


def _req():
    return {
        "id": 1, "target_server_id": 7, "database_name": "app",
        "query": "UPDATE t SET x = 1", "requester_slack_id": "U0EXAMPLE01",
    }


def test_downgraded_grant_is_blocked_before_credentials(monkeypatch):
    # Query needs RW, but the user is now only RO on this database.
    seen = _wire(monkeypatch, current_mode="ro", main_tier="rw")
    ex._run(_req(), client=None)
    assert "creds" not in seen, "must not fetch credentials after re-auth fails"
    assert "changed after approval" in seen.get("fail", "")


def test_revoked_grant_is_blocked(monkeypatch):
    # No grant covers this database anymore.
    seen = _wire(monkeypatch, current_mode=None, main_tier="ro")
    ex._run(_req(), client=None)
    assert "creds" not in seen
    assert "changed after approval" in seen.get("fail", "")


def test_sufficient_grant_passes_reauth(monkeypatch):
    # Still RW → re-auth passes and execution proceeds to credential fetch.
    seen = _wire(monkeypatch, current_mode="rw", main_tier="rw")
    ex._run(_req(), client=None)
    assert seen.get("creds") is True, "re-auth should let a still-valid grant through"
