"""`web_own_ui_super_admin_only`: QueryHub's own web sign-in, closed to everyone
but super-admins.

The switch is checked at four places: local login, the redirect callback,
refresh and every request that carries a browser session. A portal request
carries a signed assertion and never meets the switch. Each test names the
place it pins, so a removed check fails one test and says which.
"""
import asyncio

import pytest
from starlette.requests import Request

from queryhub import admins, requesters
from queryhub.web import auth_providers, deps, login_throttle, routes_auth

SUPER = "U0EXAMPLE001"
DEV = "U0EXAMPLE002"


@pytest.fixture
def switch(monkeypatch):
    """Set the switch value. `None` leaves the key unset, as on a new install."""
    state = {"value": "off"}
    real = deps.cfg.get_setting

    def fake(key, default=None):
        if key == "web_own_ui_super_admin_only":
            return default if state["value"] is None else state["value"]
        return real(key, default)

    monkeypatch.setattr(deps.cfg, "get_setting", fake)
    monkeypatch.setattr(admins, "is_super_admin", lambda p: p == SUPER)
    return state


def _cookie_request():
    return Request({
        "type": "http", "method": "GET", "path": "/api/me", "query_string": b"",
        "headers": [(b"cookie", f"{deps.SESSION_COOKIE}=tok".encode())]})


@pytest.fixture
def session_of(monkeypatch):
    def _set(sub, provider="slack"):
        monkeypatch.setattr(deps.sessions, "verify_access",
                            lambda t: {"sub": sub, "sid": "s1", "provider": provider})
        monkeypatch.setattr(deps.sessions, "session_alive",
                            lambda sid, sub=None: True)
    return _set


# ---- the helper ----------------------------------------------------------

def test_off_admits_everybody_without_asking_who_they_are(switch, monkeypatch):
    switch["value"] = "off"
    monkeypatch.setattr(admins, "is_super_admin",
                        lambda p: pytest.fail("off must not look anyone up"))
    assert deps.own_ui_allowed(DEV) is True


def test_an_unset_key_reads_as_off(switch):
    switch["value"] = None
    assert deps.own_ui_allowed(DEV) is True


@pytest.mark.parametrize("raw", ["on", " ON ", "true", "1", "yes"])
def test_on_admits_only_a_super_admin(switch, raw):
    switch["value"] = raw
    assert deps.own_ui_allowed(SUPER) is True
    assert deps.own_ui_allowed(DEV) is False


def test_a_fault_while_reading_closes_the_gate(monkeypatch):
    """An error is not an OFF. The request fails, it does not pass."""
    def boom(key, default=None):
        raise RuntimeError("bot_config is unreachable")
    monkeypatch.setattr(deps.cfg, "get_setting", boom)
    with pytest.raises(RuntimeError):
        deps.own_ui_allowed(DEV)


# ---- enforcement 1: every request with a browser session -----------------

def test_a_live_session_of_a_developer_is_refused_once_the_switch_is_on(
        switch, session_of):
    session_of(DEV)
    switch["value"] = "off"
    assert deps.current_user(_cookie_request())["sub"] == DEV
    switch["value"] = "on"
    with pytest.raises(Exception) as e:
        deps.current_user(_cookie_request())
    assert e.value.status_code == 403
    assert e.value.detail["code"] == "ui_super_admin_only"
    assert e.value.detail["message"] == deps.PORTAL_ONLY_MESSAGE


def test_a_super_admin_session_keeps_working_with_the_switch_on(switch, session_of):
    session_of(SUPER)
    switch["value"] = "on"
    assert deps.current_user(_cookie_request())["sub"] == SUPER


def test_a_portal_request_never_meets_the_switch(switch, monkeypatch):
    """Judged by its assertion only. The principal is not a super-admin."""
    switch["value"] = "on"
    monkeypatch.setattr(deps.idp_assertion, "verify",
                        lambda *a, **k: deps.idp_assertion.Principal(DEV, "Dev"))
    request = Request({
        "type": "http", "method": "GET", "path": "/api/me", "query_string": b"",
        "headers": [(deps.ASSERTION_HEADER.lower().encode(), b"tok")]})
    claims = deps.current_user(request)
    assert claims["sub"] == DEV
    assert claims["provider"] == "idp"


# ---- enforcement 2: local login ------------------------------------------

class _JsonRequest:
    headers = {"user-agent": "test"}
    client = None

    async def json(self):
        return {"username": "bob", "password": "pw"}


@pytest.fixture
def local_login(monkeypatch):
    created = []

    class _Local:
        kind = "password"

        @staticmethod
        def verify(username, password):
            return auth_providers.Identity(principal_id=DEV, email="d@example.com",
                                           name="Dev", provider="local")

    monkeypatch.setattr(auth_providers, "get_provider", lambda n: _Local())
    monkeypatch.setattr(login_throttle, "retry_after_seconds", lambda *a: 0)
    monkeypatch.setattr(login_throttle, "clear", lambda *a: None)
    monkeypatch.setattr(requesters, "is_allowed", lambda p: True)
    monkeypatch.setattr(admins, "is_admin", lambda p: False)
    monkeypatch.setattr(routes_auth.sessions, "create_session",
                        lambda *a, **k: (created.append(a), (7, "refresh"))[1])
    monkeypatch.setattr(routes_auth.sessions, "mint_access", lambda c, sid: "access")
    monkeypatch.setattr(routes_auth.audit, "log", lambda *a, **k: None)
    from queryhub import local_users
    monkeypatch.setattr(local_users, "touch_login", lambda u: None)
    return created


def test_local_login_of_a_developer_is_refused_with_the_switch_on(switch, local_login):
    switch["value"] = "on"
    with pytest.raises(Exception) as e:
        asyncio.run(routes_auth.auth_local_login(_JsonRequest()))
    assert e.value.status_code == 403
    assert e.value.detail["code"] == "ui_super_admin_only"
    assert local_login == [], "no session may exist for a refused login"


def test_local_login_works_with_the_switch_off(switch, local_login):
    switch["value"] = "off"
    resp = asyncio.run(routes_auth.auth_local_login(_JsonRequest()))
    assert resp.status_code == 200
    assert len(local_login) == 1


# ---- enforcement 3: the redirect callback --------------------------------

class _CallbackRequest:
    def __init__(self, state):
        self.cookies = {routes_auth.OAUTH_STATE_COOKIE: state}
        self.headers = {}
        self.client = None


@pytest.fixture
def callback(monkeypatch):
    state = auth_providers.make_state()
    principal = {"id": DEV}
    created = []

    class _P:
        name = "corp"
        kind = "oauth"
        label = "Corp"

        @staticmethod
        def enabled():
            return True

        def exchange(self, code, redirect_uri, st=""):
            return auth_providers.Identity(principal_id=principal["id"],
                                           email="d@example.com", name="Dev",
                                           provider="corp")

    monkeypatch.setattr(auth_providers, "get_provider", lambda n: _P())
    monkeypatch.setattr(admins, "is_admin", lambda p: False)
    monkeypatch.setattr(requesters, "is_allowed", lambda p: True)
    monkeypatch.setattr(deps, "employment_verdict", lambda uid: "active")
    monkeypatch.setattr(routes_auth, "base_url", lambda: "https://qh.test")
    monkeypatch.setattr(routes_auth.sessions, "create_session",
                        lambda *a, **k: (created.append(a), (7, "refresh"))[1])
    monkeypatch.setattr(routes_auth.sessions, "mint_access", lambda c, sid: "access")
    monkeypatch.setattr(routes_auth.audit, "log", lambda *a, **k: None)

    def run(who):
        principal["id"] = who
        return routes_auth.auth_callback("corp", _CallbackRequest(state),
                                         code="c", state=state)
    run.created = created
    return run


def test_an_sso_callback_of_a_developer_goes_back_to_the_sign_in_screen(
        switch, callback):
    switch["value"] = "on"
    resp = callback(DEV)
    assert resp.status_code == 302
    assert "auth_error=portal_only" in resp.headers["location"]
    assert callback.created == [], "no session may exist for a refused login"


def test_an_sso_callback_of_a_super_admin_signs_in(switch, callback):
    switch["value"] = "on"
    resp = callback(SUPER)
    assert "auth_error" not in resp.headers["location"]
    assert len(callback.created) == 1


def test_an_sso_callback_works_with_the_switch_off(switch, callback):
    switch["value"] = "off"
    assert "auth_error" not in callback(DEV).headers["location"]


# ---- enforcement 4: refresh ----------------------------------------------

class _RefreshRequest:
    cookies = {deps.REFRESH_COOKIE: "refresh-token"}
    headers = {}
    client = None


@pytest.fixture
def refresh(monkeypatch):
    revoked = []
    state = {"uid": DEV}

    monkeypatch.setattr(routes_auth.sessions, "rotate_refresh",
                        lambda t: {"id": 9, "slack_user_id": state["uid"],
                                   "auth_provider": "slack",
                                   "refresh_token": "next", "avatar_url": None})
    monkeypatch.setattr(routes_auth.sessions, "revoke_session",
                        lambda sid, why: revoked.append((sid, why)))
    monkeypatch.setattr(routes_auth.sessions, "mint_access", lambda c, sid: "access")
    monkeypatch.setattr(deps, "employment_verdict", lambda uid: "active")
    monkeypatch.setattr(admins, "is_admin", lambda p: False)
    monkeypatch.setattr(requesters, "is_allowed", lambda p: True)
    monkeypatch.setattr(requesters, "get", lambda p: {"name": "Dev", "email": None})
    monkeypatch.setattr(admins, "get", lambda p: None)

    def run(who):
        state["uid"] = who
        return routes_auth.auth_refresh(_RefreshRequest())
    run.revoked = revoked
    return run


def test_refresh_ends_the_session_of_a_developer_with_the_switch_on(switch, refresh):
    switch["value"] = "on"
    with pytest.raises(Exception) as e:
        refresh(DEV)
    assert e.value.status_code == 403
    assert e.value.detail["code"] == "ui_super_admin_only"
    assert [sid for sid, _ in refresh.revoked] == [9]


def test_refresh_keeps_a_super_admin_signed_in(switch, refresh):
    switch["value"] = "on"
    assert refresh(SUPER).status_code == 200
    assert refresh.revoked == []


def test_refresh_works_with_the_switch_off(switch, refresh):
    switch["value"] = "off"
    assert refresh(DEV).status_code == 200
    assert refresh.revoked == []
