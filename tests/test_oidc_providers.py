"""Generic OIDC login providers: discovery from the environment, the
per-attempt PKCE/nonce derivation, and the email→principal mapping that
decides whose grants a login lands on.

No network and no DB: discovery and the requesters/admins lookups are
monkeypatched. What is exercised is our own logic — which providers get
offered, what the authorization URL asks for, and every way the mapping
is supposed to refuse.
"""
import os

os.environ.setdefault("WEB_SESSION_SECRET", "test-secret-not-for-prod")

import base64  # noqa: E402
import hashlib  # noqa: E402
import urllib.parse  # noqa: E402

import pytest  # noqa: E402

from dba_slack_bot import config as cfg  # noqa: E402
from dba_slack_bot.web import auth_providers as ap  # noqa: E402

DISCOVERY = {
    "issuer": "https://sso.example.test/application/o/queryhub/",
    "authorization_endpoint": "https://sso.example.test/application/o/authorize/",
    "token_endpoint": "https://sso.example.test/application/o/token/",
    "jwks_uri": "https://sso.example.test/application/o/queryhub/jwks/",
    "id_token_signing_alg_values_supported": ["RS256"],
}


@pytest.fixture
def corp(monkeypatch):
    """One fully configured provider named `corp`, with discovery stubbed."""
    monkeypatch.setenv("OIDC_CORP_ISSUER", DISCOVERY["issuer"])
    monkeypatch.setenv("OIDC_CORP_CLIENT_ID", "client-abc")
    monkeypatch.setenv("OIDC_CORP_CLIENT_SECRET", "shh")
    monkeypatch.setattr(ap, "_discover", lambda issuer: DISCOVERY)
    return ap.OIDCProvider("corp", "OIDC_CORP")


# ---- discovery of providers from the environment ----------------------------

def test_a_configured_provider_is_registered_and_offered(corp, monkeypatch):
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: d)
    assert "corp" in ap.oidc_ids()
    assert "corp" in ap.enabled_providers()
    assert ap.get_provider("corp") is not None


def test_built_ins_survive_a_configured_provider(corp, monkeypatch):
    """The whole point of N providers: adding one takes nothing away."""
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: d)
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csec")
    offered = ap.enabled_providers()
    assert {"slack", "corp"} <= set(offered)


def test_two_providers_coexist(monkeypatch):
    for tok in ("ONE", "TWO"):
        monkeypatch.setenv(f"OIDC_{tok}_ISSUER", DISCOVERY["issuer"])
        monkeypatch.setenv(f"OIDC_{tok}_CLIENT_ID", "c")
        monkeypatch.setenv(f"OIDC_{tok}_CLIENT_SECRET", "s")
    assert {"one", "two"} <= set(ap.oidc_ids())


def test_half_configured_provider_is_not_offered(monkeypatch):
    """Issuer only. A login button that cannot complete is worse than none."""
    monkeypatch.setenv("OIDC_PARTIAL_ISSUER", DISCOVERY["issuer"])
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: d)
    assert "partial" in ap.oidc_ids()          # it is described...
    assert "partial" not in ap.enabled_providers()   # ...but not offered
    assert ap.get_provider("partial") is None


def test_runtime_switch_turns_one_provider_off(corp, monkeypatch):
    monkeypatch.setattr(cfg, "get_setting",
                        lambda k, d=None: "off" if k == "web_auth_corp_enabled" else d)
    assert "corp" not in ap.enabled_providers()
    assert ap.get_provider("corp") is None


def test_a_built_in_id_cannot_be_shadowed_from_the_environment(monkeypatch):
    """`slack` from the environment would replace the workspace-gated
    provider with a generic one — silently, and only in production."""
    monkeypatch.setenv("OIDC_SLACK_ISSUER", DISCOVERY["issuer"])
    monkeypatch.setenv("OIDC_SLACK_CLIENT_ID", "c")
    monkeypatch.setenv("OIDC_SLACK_CLIENT_SECRET", "s")
    assert "slack" not in ap.oidc_ids()
    assert isinstance(ap._registry()["slack"], ap.SlackOIDC)


# ---- the authorization request ---------------------------------------------

def test_start_asks_for_code_pkce_and_nonce(corp):
    state = ap.make_state()
    q = urllib.parse.parse_qs(
        urllib.parse.urlparse(corp.start("https://qh.test/cb", state)).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["client-abc"]
    assert q["redirect_uri"] == ["https://qh.test/cb"]
    assert q["state"] == [state]
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["openid email profile"]
    # The challenge must be the S256 of the verifier the callback will send.
    verifier = ap._derive(state, "pkce")
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert q["code_challenge"] == [expect]
    assert q["nonce"] == [ap._derive(state, "nonce")]


def test_pkce_and_nonce_differ_and_are_state_bound(corp):
    """Same attempt, two purposes: reusing one value for both would let a
    captured nonce be replayed as the verifier."""
    s1, s2 = ap.make_state(), ap.make_state()
    assert ap._derive(s1, "pkce") != ap._derive(s1, "nonce")
    assert ap._derive(s1, "pkce") != ap._derive(s2, "pkce")
    # A verifier must be 43-128 chars of unreserved characters (RFC 7636).
    assert 43 <= len(ap._derive(s1, "pkce")) <= 128


# ---- the email → principal mapping ------------------------------------------

def _claims(**over):
    base = {"email": "dev@example.com", "email_verified": True, "name": "Dev"}
    base.update(over)
    return base


def _map(corp, monkeypatch, claims, requester=None, admin=None, domain=""):
    """Run only the post-verification half of exchange() — the part that
    turns verified claims into a principal — with the token round-trip and
    signature check stubbed out."""
    from dba_slack_bot import admins, requesters
    monkeypatch.setattr(cfg, "get_setting",
                        lambda k, d=None: domain if k == "web_allowed_email_domain" else d)
    monkeypatch.setattr(requesters, "by_email", lambda e: requester)
    monkeypatch.setattr(admins, "by_email", lambda e: admin)
    return corp._identity_from_claims(claims)


ROW = {"slack_user_id": "U0REALUSER1", "name": "Dev", "enabled": True}


def test_email_maps_to_the_existing_principal(corp, monkeypatch):
    ident = _map(corp, monkeypatch, _claims(), requester=ROW)
    assert ident.principal_id == "U0REALUSER1"
    assert ident.provider == "corp"
    assert ident.email == "dev@example.com"


def test_an_admin_without_a_requester_row_still_maps(corp, monkeypatch):
    """A DBA who only approves has no requesters row — refusing them would
    lock out exactly the people who operate this."""
    ident = _map(corp, monkeypatch, _claims(), requester=None, admin=ROW)
    assert ident.principal_id == "U0REALUSER1"


def test_an_unknown_address_is_refused_not_onboarded(corp, monkeypatch):
    with pytest.raises(ap.AuthError) as e:
        _map(corp, monkeypatch, _claims(), requester=None, admin=None)
    assert e.value.code == "not_onboarded"


def test_an_unverified_email_is_refused(corp, monkeypatch):
    """The address is the join to someone's grants. If the provider will
    not vouch for it, a user could type a colleague's."""
    with pytest.raises(ap.AuthError) as e:
        _map(corp, monkeypatch, _claims(email_verified=False), requester=ROW)
    assert e.value.code == "email_unverified"


def test_a_missing_email_is_refused(corp, monkeypatch):
    with pytest.raises(ap.AuthError) as e:
        _map(corp, monkeypatch, _claims(email=None), requester=ROW)
    assert e.value.code == "no_email"


def test_the_domain_gate_still_applies(corp, monkeypatch):
    with pytest.raises(ap.AuthError) as e:
        _map(corp, monkeypatch, _claims(), requester=ROW, domain="other.test")
    assert e.value.code == "email_domain"


def test_email_is_normalised_before_lookup(corp, monkeypatch):
    seen = []
    from dba_slack_bot import admins, requesters
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: d)
    monkeypatch.setattr(requesters, "by_email",
                        lambda e: (seen.append(e), ROW)[1])
    monkeypatch.setattr(admins, "by_email", lambda e: None)
    ident = corp._identity_from_claims(_claims(email="  Dev@Example.COM  "))
    assert seen == ["dev@example.com"]
    assert ident.email == "dev@example.com"


# ---- signature-algorithm allow-list -----------------------------------------

def test_hmac_and_none_are_never_accepted():
    """With HS256 the client secret doubles as the verification key, so
    anyone holding it could mint identities; `none` needs no key at all."""
    for bad in ("none", "HS256", "HS512"):
        assert bad not in ap._SAFE_ALGS
