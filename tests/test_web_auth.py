"""Pure tests for the web auth layer: signed OAuth state, access-JWT
mint/verify, provider registry toggle. No DB, no network."""
import os
import time

os.environ.setdefault("WEB_SESSION_SECRET", "test-secret-not-for-prod")

import pytest  # noqa: E402

from dba_slack_bot import config as cfg  # noqa: E402
from dba_slack_bot.web import auth_providers, sessions  # noqa: E402


# ---- OAuth state ------------------------------------------------------------

def test_state_roundtrip():
    s = auth_providers.make_state()
    assert auth_providers.check_state(s)


def test_state_tamper_rejected():
    s = auth_providers.make_state()
    assert not auth_providers.check_state(s[:-4] + "beef")


def test_state_expiry(monkeypatch):
    s = auth_providers.make_state()
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 3600)
    assert not auth_providers.check_state(s)


def test_state_garbage():
    assert not auth_providers.check_state("")
    assert not auth_providers.check_state("a.b")
    assert not auth_providers.check_state(None)


# ---- access JWT -------------------------------------------------------------

IDENT = {"slack_user_id": "U0TESTUSER1", "name": "Test User",
         "email": "t@example.com", "provider": "slack"}


def test_jwt_roundtrip():
    tok = sessions.mint_access(IDENT, session_id=42)
    claims = sessions.verify_access(tok)
    assert claims["sub"] == "U0TESTUSER1"
    assert claims["sid"] == 42
    assert claims["provider"] == "slack"


def test_jwt_tamper_rejected():
    tok = sessions.mint_access(IDENT, session_id=42)
    assert sessions.verify_access(tok + "x") is None
    assert sessions.verify_access("garbage") is None


def test_jwt_expiry(monkeypatch):
    monkeypatch.setattr(sessions, "access_ttl_minutes", lambda: -1)
    tok = sessions.mint_access(IDENT, session_id=1)
    assert sessions.verify_access(tok) is None


# ---- provider registry toggle ----------------------------------------------

def _with_toggle(monkeypatch, value, *, configured=True):
    """Set the toggle, and by default also the Slack app credentials.

    The provider is offered only when it is BOTH switched on and configured, so
    a toggle-only fixture would no longer describe a usable provider. `configured
    =False` is the fresh-install shape: toggle on (it defaults on), no Slack app.
    """
    def fake(key, default=None):
        if key == "web_auth_slack_enabled":
            return value
        return default if default is not None else ""
    monkeypatch.setattr(cfg, "get_setting", fake)
    if configured:
        monkeypatch.setenv("SLACK_CLIENT_ID", "1234.5678")
        monkeypatch.setenv("SLACK_CLIENT_SECRET", "test-secret")
    else:
        monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
        monkeypatch.delenv("SLACK_CLIENT_SECRET", raising=False)


def test_registry_on(monkeypatch):
    _with_toggle(monkeypatch, "on")
    assert "slack" in auth_providers.enabled_providers()
    assert auth_providers.get_provider("slack") is not None


def test_slack_is_not_offered_without_credentials(monkeypatch):
    """The first-run defect. The toggle defaults to 'on', so a clean install with
    no Slack app advertised "Sign in with Slack" on its login screen and the
    click raised provider_unconfigured — a dead end as the very first thing a new
    operator is shown. Measured on a container install 2026-07-31:
    /api/auth/providers listed slack with no SLACK_CLIENT_ID set."""
    _with_toggle(monkeypatch, "on", configured=False)
    assert auth_providers.enabled_providers() == {}
    assert auth_providers.get_provider("slack") is None


def test_registry_off(monkeypatch):
    _with_toggle(monkeypatch, "off")
    assert auth_providers.enabled_providers() == {}
    assert auth_providers.get_provider("slack") is None


def test_unknown_provider(monkeypatch):
    _with_toggle(monkeypatch, "on")
    assert auth_providers.get_provider("idp") is None


def test_unconfigured_client_id_raises(monkeypatch):
    """Defence in depth, still worth keeping: the registry no longer hands out an
    unconfigured provider, but `/api/auth/slack/start` is a real route and could
    be reached another way. Built directly rather than via the registry, because
    the registry's job is now to not return this at all."""
    _with_toggle(monkeypatch, "on", configured=False)
    p = auth_providers.SlackOIDC()
    with pytest.raises(auth_providers.AuthError) as e:
        p.start("http://localhost:8080/api/auth/slack/callback",
                auth_providers.make_state())
    assert e.value.code == "provider_unconfigured"


# ---- allowlist fail-closed (empty requesters table must NOT open access) ----

def test_allowlist_fails_closed_when_empty(monkeypatch):
    """The critical regression guard: a non-admin whose id isn't in the
    requesters table is rejected even if the whole table is empty. (This used
    to return True — "empty allowlist == open mode" — a fail-open footgun.)"""
    from dba_slack_bot import requesters
    monkeypatch.setattr(requesters.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(requesters.db, "fetch_one", lambda *a, **k: None)
    assert requesters.is_allowed("U_RANDOM") is False


def test_allowlist_admin_always_allowed(monkeypatch):
    from dba_slack_bot import requesters
    monkeypatch.setattr(requesters.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(requesters.db, "fetch_one", lambda *a, **k: None)
    assert requesters.is_allowed("U_ADMIN") is True


def test_allowlist_enabled_requester_allowed(monkeypatch):
    from dba_slack_bot import requesters
    monkeypatch.setattr(requesters.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(requesters.db, "fetch_one", lambda *a, **k: {"1": 1})
    assert requesters.is_allowed("U_REQ") is True
