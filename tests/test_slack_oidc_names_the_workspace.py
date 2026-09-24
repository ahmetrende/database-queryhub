"""The Slack sign-in must name the workspace it wants.

Slack's own documentation: *"If that workspace has been previously
authenticated, the user will be signed in directly, bypassing the consent
screen."* Without `team`, Slack cannot know which workspace is meant, so it
renders the account picker — and because it has to ask, it shows the whole
consent screen again. A returning user saw that after every logout, on an app
they had already authorised.

It is also the stricter flow, not just the quieter one. `exchange()` already
refuses an id_token from another workspace (`wrong_workspace`); naming the team
up front stops somebody with two Slack accounts picking the wrong one and
finding out a redirect later.
"""
from __future__ import annotations

import urllib.parse as up

import pytest

from queryhub.web import auth_providers as ap


def _params(monkeypatch, team):
    monkeypatch.setattr(ap, "_workspace_team_id", lambda: team)
    monkeypatch.setattr(ap.SlackOIDC, "_client_id", lambda self: "CID")
    url = ap.SlackOIDC().start("https://example/callback", "STATE")
    assert url.startswith(ap._SLACK_AUTHORIZE + "?")
    return dict(up.parse_qsl(up.urlparse(url).query))


def test_the_workspace_is_named(monkeypatch):
    assert _params(monkeypatch, "T0DEADBEEF")["team"] == "T0DEADBEEF"


def test_it_is_the_same_id_the_exchange_gates_on(monkeypatch):
    """If these two ever came from different places, the sign-in would send a
    user to a workspace the callback then rejects."""
    seen = []
    monkeypatch.setattr(ap, "_workspace_team_id", lambda: seen.append(1) or "T1")
    monkeypatch.setattr(ap.SlackOIDC, "_client_id", lambda self: "CID")
    ap.SlackOIDC().start("https://example/callback", "STATE")
    assert seen, "start() must read the workspace id, not hardcode one"


def test_an_unavailable_workspace_id_omits_the_parameter(monkeypatch):
    """auth.test can fail. The redirect still goes out, with the picker it had
    before, rather than sending `team=None`. The callback then refuses the
    sign-in unless the workspace is known by then (see the tests below): the
    parameter is a convenience, the exchange is the gate."""
    q = _params(monkeypatch, None)
    assert "team" not in q


@pytest.mark.parametrize("key,value", [
    ("response_type", "code"),
    ("scope", "openid email profile"),
    ("redirect_uri", "https://example/callback"),
    ("state", "STATE"),
])
def test_the_rest_of_the_request_is_unchanged(monkeypatch, key, value):
    assert _params(monkeypatch, "T1")[key] == value


def test_no_prompt_parameter_is_sent(monkeypatch):
    """We never ask Slack to re-prompt. If the consent screen returns, the
    cause is upstream, not a flag of ours — worth pinning so a future edit
    cannot reintroduce the very thing this fixed."""
    assert "prompt" not in _params(monkeypatch, "T1")


# --- the exchange is the gate, and it fails closed ---------------------------


@pytest.fixture
def exchange(monkeypatch):
    """`SlackOIDC.exchange` with Slack faked: the token call, the JWKS and the
    id_token decode. Returns a function taking the workspace the resolver
    reports and the team the id_token names."""
    import sys
    import types

    def run(expected, token_team):
        monkeypatch.setattr(ap, "_workspace_team_id", lambda: expected)
        monkeypatch.setattr(ap.SlackOIDC, "_client_id", lambda self: "CID")
        monkeypatch.setattr(ap.SlackOIDC, "_client_secret", lambda self: "SEC")
        httpx = types.ModuleType("httpx")
        httpx.post = lambda *a, **k: types.SimpleNamespace(
            json=lambda: {"ok": True, "id_token": "tok"})
        monkeypatch.setitem(sys.modules, "httpx", httpx)
        monkeypatch.setattr(ap.jwt, "PyJWKClient", lambda url: types.SimpleNamespace(
            get_signing_key_from_jwt=lambda t: types.SimpleNamespace(key="k")))
        monkeypatch.setattr(ap.jwt, "decode", lambda *a, **k: {
            "https://slack.com/team_id": token_team,
            "https://slack.com/user_id": "U0EXAMPLE001",
            "email": "someone@example.com", "name": "Someone"})
        monkeypatch.setattr(ap.cfg, "get_setting", lambda key, default=None: "")
        return ap.SlackOIDC().exchange("code", "https://example/callback")
    return run


def test_the_right_workspace_signs_in(exchange):
    assert exchange("T1", "T1").principal_id == "U0EXAMPLE001"


def test_another_workspace_is_refused(exchange):
    with pytest.raises(ap.AuthError) as e:
        exchange("T1", "T2")
    assert e.value.code == "wrong_workspace"


def test_an_unknown_workspace_refuses_the_sign_in(exchange):
    """It used to skip the comparison, so whenever auth.test failed an account
    from any workspace got past the gate."""
    with pytest.raises(ap.AuthError) as e:
        exchange(None, "T-anything")
    assert e.value.code == "workspace_unknown"


def test_a_pinned_workspace_wins_and_needs_no_bot_token(monkeypatch):
    """For an install that runs Slack sign-in without the bot."""
    monkeypatch.setattr(ap.cfg, "get_setting",
                        lambda key, default=None: "T0PINNED" if key == "web_slack_team_id" else default)
    monkeypatch.setattr(ap, "_TEAM_ID_CACHE", None)
    assert ap._workspace_team_id() == "T0PINNED"
