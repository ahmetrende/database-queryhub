"""Web session liveness + must-change-password gate.

- current_user rejects a session whose local account has been disabled, on
  the very next request (no wait for token expiry / explicit revoke).
- block_if_password_change_required (wired as a router-level dependency on
  every action router) blocks a must_change_pw local account.
Slack sessions are unaffected by both.
"""
import pytest

from queryhub import local_users
from queryhub.web import deps


def _req():
    return type("R", (), {"cookies": {"qh_session": "tok"}, "headers": {}})()


def _session(monkeypatch, claims):
    monkeypatch.setattr(deps.sessions, "verify_access", lambda t: claims)
    monkeypatch.setattr(deps.sessions, "session_alive", lambda sid: True)


def test_disabled_local_account_is_rejected(monkeypatch):
    _session(monkeypatch, {"sub": "local:bob", "sid": "s1", "provider": "local"})
    monkeypatch.setattr(local_users, "username_of", lambda sub: "bob")
    monkeypatch.setattr(local_users, "get", lambda u: {"enabled": False})
    with pytest.raises(deps.HTTPException) as ei:
        deps.current_user(_req())
    assert ei.value.status_code == 401


def test_enabled_local_account_passes(monkeypatch):
    _session(monkeypatch, {"sub": "local:bob", "sid": "s1", "provider": "local"})
    monkeypatch.setattr(local_users, "username_of", lambda sub: "bob")
    monkeypatch.setattr(local_users, "get", lambda u: {"enabled": True})
    assert deps.current_user(_req())["provider"] == "local"


def test_slack_session_skips_local_liveness(monkeypatch):
    _session(monkeypatch, {"sub": "U0EXAMPLE01", "sid": "s1", "provider": "slack"})
    # local_users.get would raise if called — assert it isn't for a slack sub.
    monkeypatch.setattr(local_users, "get",
                        lambda u: (_ for _ in ()).throw(AssertionError("called")))
    assert deps.current_user(_req())["sub"] == "U0EXAMPLE01"


def test_pw_gate_blocks_must_change_local(monkeypatch):
    monkeypatch.setattr(local_users, "username_of", lambda s: "bob")
    monkeypatch.setattr(local_users, "get", lambda u: {"must_change_pw": True})
    with pytest.raises(deps.HTTPException) as ei:
        deps.block_if_password_change_required(
            {"sub": "local:bob", "provider": "local"})
    assert ei.value.status_code == 403


def test_pw_gate_noop_for_slack(monkeypatch):
    # No local lookup, no raise.
    deps.block_if_password_change_required({"sub": "U0EXAMPLE01", "provider": "slack"})
