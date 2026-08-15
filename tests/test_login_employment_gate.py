"""The offboarding check at LOGIN, not just at refresh.

While Slack was the only redirect provider, `slack_employment_ok` at refresh
was enough: Slack does not authenticate a deactivated account, so the login
leg could not be the hole. An external SSO removes that guarantee — the
company IdP can still authenticate someone Slack has deactivated, and the
`requesters` row admitting them stays enabled until a human removes it.

These pin the gate on both legs, and pin that `local` stays exempt (its
principals have no Slack identity to ask about).
"""
import pytest

from queryhub.web import auth_providers, deps, routes_auth


class _Resp:
    """Enough of a Request for the callback: cookies + headers."""
    def __init__(self, state):
        self.cookies = {routes_auth.OAUTH_STATE_COOKIE: state}
        self.headers = {}
        self.client = None


def _identity(provider):
    return auth_providers.Identity(principal_id="U0GONEUSER1", email="x@y.z",
                                   name="Gone", provider=provider)


@pytest.fixture
def wired(monkeypatch):
    """A callback that would otherwise succeed: state valid, provider
    enabled, principal whitelisted. Only the employment answer varies."""
    from queryhub import admins, requesters

    state = auth_providers.make_state()

    class _P:
        name = "corp"
        kind = "oauth"
        label = "Corp"

        @staticmethod
        def enabled():
            return True

        def exchange(self, code, redirect_uri, st=""):
            return _identity(self.name)

    monkeypatch.setattr(auth_providers, "get_provider", lambda n: _P())
    monkeypatch.setattr(admins, "is_admin", lambda p: False)
    monkeypatch.setattr(requesters, "is_allowed", lambda p: True)
    monkeypatch.setattr(routes_auth, "base_url", lambda: "https://qh.test")
    return state


def test_a_deactivated_account_cannot_sign_in_via_external_sso(wired, monkeypatch):
    monkeypatch.setattr(deps, "slack_employment_ok", lambda uid: False)
    resp = routes_auth.auth_callback("corp", _Resp(wired), code="c", state=wired)
    assert resp.status_code == 302
    assert "auth_error=account_gone" in resp.headers["location"]
    # and crucially: no session cookie was handed out on the way past
    assert not any(deps.SESSION_COOKIE in v
                   for k, v in resp.raw_headers or [] if k == b"set-cookie")


def test_an_employed_account_signs_in(wired, monkeypatch):
    called = []
    monkeypatch.setattr(deps, "slack_employment_ok",
                        lambda uid: (called.append(uid), True)[1])
    monkeypatch.setattr(routes_auth.sessions, "create_session",
                        lambda *a, **k: (7, "refresh-tok"))
    monkeypatch.setattr(routes_auth.sessions, "mint_access",
                        lambda claims, sid: "access-tok")
    monkeypatch.setattr(routes_auth.audit, "log", lambda *a, **k: None)
    resp = routes_auth.auth_callback("corp", _Resp(wired), code="c", state=wired)
    assert resp.status_code == 302
    assert "auth_error" not in resp.headers["location"]
    assert called == ["U0GONEUSER1"], "the check must actually run"


def test_the_check_is_skipped_for_local_principals(wired, monkeypatch):
    """`local:<username>` is not a Slack id — asking users.info about it
    would fail closed and lock every vanilla install out of its own app."""
    from queryhub import requesters

    class _Local:
        name = "local"
        kind = "oauth"      # forced through the redirect leg on purpose
        label = "Local"

        @staticmethod
        def enabled():
            return True

        def exchange(self, code, redirect_uri, st=""):
            return auth_providers.Identity(principal_id="local:bob", email=None,
                                           name="Bob", provider="local")

    monkeypatch.setattr(auth_providers, "get_provider", lambda n: _Local())
    monkeypatch.setattr(requesters, "is_allowed", lambda p: True)
    asked = []
    monkeypatch.setattr(deps, "slack_employment_ok",
                        lambda uid: (asked.append(uid), False)[1])
    monkeypatch.setattr(routes_auth.sessions, "create_session",
                        lambda *a, **k: (7, "refresh-tok"))
    monkeypatch.setattr(routes_auth.sessions, "mint_access",
                        lambda claims, sid: "access-tok")
    monkeypatch.setattr(routes_auth.audit, "log", lambda *a, **k: None)
    resp = routes_auth.auth_callback("local", _Resp(wired), code="c", state=wired)
    assert asked == [], "users.info must not be asked about a local principal"
    assert "auth_error" not in resp.headers["location"]
