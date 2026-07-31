"""Pluggable login providers for QueryHub Web.

Whatever the login method, the canonical identity is the SLACK USER ID —
grants, audit and PII exemptions all hang off it. A provider's only job
is to produce that identity safely:

    start(redirect_uri)            -> browser redirect URL
    exchange(code, redirect_uri)   -> Identity(slack_user_id, email, name)

Providers are toggled at runtime via bot_config (`web_auth_slack_enabled`
today; a future `web_auth_idp_enabled` provider maps IDP identities to
Slack ids via requesters.email). GET /auth/providers feeds the login
screen from the same registry, so enabling a provider is one config flip.

Slack employment verification (users.info at refresh + before RW/DDL) is
NOT part of a provider — it always runs, whoever did the login.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets as pysecrets
import time
import urllib.parse
from dataclasses import dataclass

import jwt

from .. import config as cfg
from . import sessions

log = logging.getLogger(__name__)

_SLACK_AUTHORIZE = "https://slack.com/openid/connect/authorize"
_SLACK_JWKS = "https://slack.com/openid/connect/keys"
_SLACK_ISS = "https://slack.com"


@dataclass(frozen=True)
class Identity:
    principal_id: str
    email: str | None
    name: str | None
    provider: str
    avatar: str | None = None


class AuthError(Exception):
    """Login-flow failure with a short, user-safe reason code."""
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


# ---- signed state (CSRF protection for the OAuth round-trip) ---------------

def _state_secret() -> bytes:
    return hmac.new(sessions._signing_secret(), b"oauth-state", hashlib.sha256).digest()


def make_state() -> str:
    ts = str(int(time.time()))
    nonce = pysecrets.token_urlsafe(12)
    sig = hmac.new(_state_secret(), f"{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{nonce}.{sig}"


def check_state(state: str | None, max_age_sec: int = 600) -> bool:
    if not state:
        return False
    try:
        ts, nonce, sig = state.split(".", 2)
        expect = hmac.new(_state_secret(), f"{ts}.{nonce}".encode(),
                          hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, expect) and (time.time() - int(ts)) < max_age_sec
    except (ValueError, TypeError):
        return False


# ---- Slack OIDC provider ----------------------------------------------------

class SlackOIDC:
    name = "slack"
    label = "Sign in with Slack"
    # "oauth" = browser redirect round-trip (start/callback); "password" =
    # a username/password form posted to /api/auth/local/login. The login
    # screen renders the right control per provider from this hint.
    kind = "oauth"

    @staticmethod
    def enabled() -> bool:
        """On AND actually configured.

        The toggle defaults to 'on', so a fresh install with no Slack app at all
        advertised "Sign in with Slack" on its login screen, and clicking it
        raised provider_unconfigured. That is the FIRST screen a new operator
        sees and the first thing it offered was a dead end. Measured on a clean
        container install 2026-07-31: /api/auth/providers listed the slack
        provider with no SLACK_CLIENT_ID in the environment.

        Offering a provider only when it can actually be performed is the same
        fail-closed rule the rest of the gateway follows. An operator who flipped
        the toggle but has not set the credentials now sees the local form
        instead of a broken button, and the log line says why.
        """
        val = (cfg.get_setting("web_auth_slack_enabled", "on") or "").strip().lower()
        if val not in {"on", "1", "true", "yes"}:
            return False
        if not (os.environ.get("SLACK_CLIENT_ID")
                and os.environ.get("SLACK_CLIENT_SECRET")):
            log.info("Slack login is enabled in bot_config but SLACK_CLIENT_ID / "
                     "SLACK_CLIENT_SECRET are unset — not offering it on the "
                     "login screen.")
            return False
        return True

    @staticmethod
    def _client_id() -> str:
        cid = os.environ.get("SLACK_CLIENT_ID")
        if not cid:
            raise AuthError("provider_unconfigured",
                            "SLACK_CLIENT_ID is not set on the host")
        return cid

    @staticmethod
    def _client_secret() -> str:
        sec = os.environ.get("SLACK_CLIENT_SECRET")
        if not sec:
            raise AuthError("provider_unconfigured",
                            "SLACK_CLIENT_SECRET is not set on the host")
        return sec

    def start(self, redirect_uri: str, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self._client_id(),
            "scope": "openid email profile",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{_SLACK_AUTHORIZE}?{urllib.parse.urlencode(params)}"

    def exchange(self, code: str, redirect_uri: str) -> Identity:
        """Exchange the code, verify the id_token (signature via Slack's
        JWKS + issuer + audience), enforce the workspace gate."""
        import httpx

        resp = httpx.post(
            "https://slack.com/api/openid.connect.token",
            data={
                "client_id": self._client_id(),
                "client_secret": self._client_secret(),
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        payload = resp.json()
        if not payload.get("ok"):
            raise AuthError("token_exchange_failed", str(payload.get("error")))
        id_token = payload.get("id_token")
        if not id_token:
            raise AuthError("no_id_token",
                            "Slack returned no id_token — is the OpenID "
                            "Connect scope granted?")

        signing_key = jwt.PyJWKClient(_SLACK_JWKS).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self._client_id(),
            issuer=_SLACK_ISS,
        )

        team_id = claims.get("https://slack.com/team_id")
        expected_team = _workspace_team_id()
        if expected_team and team_id != expected_team:
            raise AuthError("wrong_workspace",
                            f"team {team_id} != workspace {expected_team}")

        domain = (cfg.get_setting("web_allowed_email_domain", "") or "").strip()
        email = claims.get("email")
        if domain and not (email or "").lower().endswith("@" + domain.lower()):
            raise AuthError("email_domain", f"email {email!r} not in @{domain}")

        return Identity(
            principal_id=claims["https://slack.com/user_id"],
            email=email,
            name=claims.get("name"),
            provider=self.name,
            # Slack's OIDC id_token carries the avatar; image_192 is the
            # sweet spot for a nav-bar avatar. `picture` is the generic
            # OIDC fallback. Absent → the UI shows initials.
            avatar=(claims.get("https://slack.com/user_image_192")
                    or claims.get("picture")),
        )


_TEAM_ID_CACHE: str | None = None


def _workspace_team_id() -> str | None:
    """The bot's own workspace, discovered once via auth.test — the
    OIDC team gate compares against this, so no extra config key."""
    global _TEAM_ID_CACHE
    if _TEAM_ID_CACHE is None:
        try:
            from slack_sdk import WebClient
            _TEAM_ID_CACHE = WebClient(
                token=cfg.ENV.slack_bot_token).auth_test()["team_id"]
        except Exception:
            log.exception("auth.test failed — workspace gate disabled this call")
            return None
    return _TEAM_ID_CACHE


# ---- local (built-in username/password) provider ---------------------------

class LocalPassword:
    """Built-in accounts for the vanilla profile (no Slack, no external IdP).

    Not an OAuth round-trip: the login screen posts credentials to
    /api/auth/local/login, which calls verify() below. The resulting
    principal id is `local:<username>` — a first-class identity in the same
    namespace as Slack ids (see local_users / migration 075)."""
    name = "local"
    label = "Sign in with a local account"
    kind = "password"

    @staticmethod
    def enabled() -> bool:
        val = (cfg.get_setting("web_auth_local_enabled", "off") or "").strip().lower()
        return val in {"on", "1", "true", "yes"}

    def verify(self, username: str, password: str) -> Identity:
        """Username/password -> Identity, or AuthError('bad_credentials').
        The error is intentionally uniform (unknown user and wrong password
        are indistinguishable) so it can't be used to enumerate accounts."""
        from .. import local_users
        row = local_users.verify_login(username, password)
        if row is None:
            raise AuthError("bad_credentials", "invalid username or password")
        return Identity(
            principal_id=local_users.to_identity(row["username"]),
            email=row.get("email"),
            name=row.get("display_name") or row["username"],
            provider=self.name,
            avatar=None,
        )


# ---- registry ---------------------------------------------------------------

_ALL = {
    SlackOIDC.name: SlackOIDC(),
    LocalPassword.name: LocalPassword(),
}


def enabled_providers() -> dict[str, object]:
    return {name: p for name, p in _ALL.items() if p.enabled()}


def get_provider(name: str):
    p = _ALL.get(name)
    if p is None or not p.enabled():
        return None
    return p
