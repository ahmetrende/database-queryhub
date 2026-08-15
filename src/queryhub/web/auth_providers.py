"""Pluggable login providers for QueryHub Web.

Whatever the login method, the canonical identity is the SLACK USER ID —
grants, audit and PII exemptions all hang off it. A provider's only job
is to produce that identity safely:

    start(redirect_uri, state)            -> browser redirect URL
    exchange(code, redirect_uri, state)   -> Identity(principal_id, email, name)

`state` reaches `exchange` as well as `start` because a provider may need
per-attempt secrets on both legs of the round-trip; the generic OIDC
provider derives its PKCE verifier and nonce from it. Slack ignores it.

Providers are toggled at runtime via bot_config, and GET /auth/providers
feeds the login screen from the same registry, so enabling one is a config
flip. There are two built-ins (`slack`, `local`) and ANY NUMBER of generic
OIDC providers, each discovered from the environment:

    OIDC_<ID>_ISSUER          required — the OIDC issuer URL
    OIDC_<ID>_CLIENT_ID       required
    OIDC_<ID>_CLIENT_SECRET   required
    OIDC_<ID>_SCOPES          optional — default "openid email profile"
    OIDC_<ID>_LABEL           optional — button text on the login screen

`<ID>` is one lowercase alphanumeric token and becomes the provider id, so
`OIDC_CORP_ISSUER` serves `corp` at /api/auth/corp/{start,callback}. That
URL is what gets registered with the identity provider, which is why the id
is the operator's to choose and stable once chosen. Adding a second company
IdP is three more environment variables and nothing else; the built-ins are
untouched and keep working alongside.

Secrets live in the environment (`/etc/queryhub/web.env`), never in
bot_config: bot_config is shared by every instance on the same bot DB and
is readable through the admin config screen. bot_config carries only the
runtime switch `web_auth_<id>_enabled` and an optional
`web_auth_<id>_label`.

Slack employment verification (users.info at refresh + before RW/DDL) is
NOT part of a provider — it always runs, whoever did the login.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
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

    def exchange(self, code: str, redirect_uri: str, state: str = "") -> Identity:
        """Exchange the code, verify the id_token (signature via Slack's
        JWKS + issuer + audience), enforce the workspace gate.

        `state` is part of the provider protocol and unused here: Slack's
        flow carries no PKCE verifier or nonce of ours to check."""
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


# ---- generic OIDC providers (any number, configured per deployment) ---------

# `OIDC_CORP_ISSUER` -> provider id `corp`. One token, no underscores: with
# them, `OIDC_A_B_CLIENT_ID` could be id `a-b` key `CLIENT_ID` or id `a-b-client`
# key `ID`, and an ambiguous provider id is an ambiguous callback URL.
_OIDC_ENV_RE = re.compile(r"^OIDC_([A-Z][A-Z0-9]{0,30})_ISSUER$")
# The id lands in a URL path segment and in the audit trail. Keep it boring.
_OIDC_ID_RE = re.compile(r"^[a-z][a-z0-9]{0,30}$")
# A generic provider may not impersonate a built-in: `slack` carries the
# workspace gate and `local` is a password form, and shadowing either from the
# environment would silently replace an authentication path.
_RESERVED_IDS = frozenset({"slack", "local"})

# Signature algorithms we will accept for an id_token, intersected with what
# the provider advertises. An allow-list, so `none` and the HMAC family can
# never arrive by negotiation — with HS256 the client secret doubles as the
# verification key, and anyone holding it could mint identities.
_SAFE_ALGS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")

_DISCOVERY_TTL_SEC = 3600
_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def _discover(issuer: str) -> dict:
    """The provider's own OIDC discovery document, cached for an hour.

    Endpoints come from the issuer rather than from our config so that a
    rotation on their side (a moved token endpoint, a new signing key)
    needs no change here. Cached because it is fetched on every login leg
    and an IdP restart should not become our outage."""
    now = time.time()
    hit = _discovery_cache.get(issuer)
    if hit and (now - hit[0]) < _DISCOVERY_TTL_SEC:
        return hit[1]
    import httpx

    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True)
        resp.raise_for_status()
        doc = resp.json()
    except Exception as e:
        raise AuthError("discovery_failed", f"{url}: {e}") from e
    if not isinstance(doc, dict):
        raise AuthError("discovery_failed", f"{url}: not a JSON object")
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(key):
            raise AuthError("discovery_incomplete", f"{url} declares no {key}")
    _discovery_cache[issuer] = (now, doc)
    return doc


def _derive(state: str, purpose: str) -> str:
    """A per-attempt secret bound to the login attempt.

    The PKCE verifier and the nonce must survive a round-trip through the
    user's browser and come back to a process that stored nothing. Deriving
    both from the signed `state` — itself echoed by a host-only cookie the
    callback checks — means no server-side attempt table and no extra
    cookie, while staying unguessable: it takes the session signing secret
    to compute either one."""
    raw = hmac.new(_state_secret(), f"{purpose}|{state}".encode(),
                   hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class OIDCProvider:
    """One external OpenID Connect identity provider.

    Configured entirely from the environment (see the module docstring), so
    a deployment can carry as many as it has IdPs without a code change.

    The identity it produces is NOT a new principal: it resolves the
    provider's verified email against `requesters`/`admins` and returns the
    principal id already on that row. That is the whole point — a login is a
    new way to prove who you are, never a new authorization subject. An
    address with no row is refused, never onboarded, because the alternative
    is that everyone the company IdP knows becomes a QueryHub user.
    """
    kind = "oauth"

    def __init__(self, provider_id: str, env_prefix: str):
        self.name = provider_id
        self._prefix = env_prefix

    # Read the environment at use, not at construction: a rotated secret
    # then takes effect on the next login instead of the next restart.
    def _env(self, suffix: str, default: str = "") -> str:
        return (os.environ.get(f"{self._prefix}_{suffix}") or default).strip()

    @property
    def label(self) -> str:
        return ((cfg.get_setting(f"web_auth_{self.name}_label", "") or "").strip()
                or self._env("LABEL")
                or "Sign in with SSO")

    def configured(self) -> bool:
        return bool(self._env("ISSUER") and self._env("CLIENT_ID")
                    and self._env("CLIENT_SECRET"))

    def enabled(self) -> bool:
        """On, and actually performable.

        Same fail-closed rule the Slack provider learned: a login screen
        only offers what it can complete. The toggle defaults to on because
        setting the three secrets IS the deliberate act — the switch exists
        to turn a working provider off, not to arm one."""
        val = (cfg.get_setting(f"web_auth_{self.name}_enabled", "on")
               or "").strip().lower()
        if val not in {"on", "1", "true", "yes"}:
            return False
        if not self.configured():
            log.info("OIDC provider %r is enabled but %s_ISSUER / _CLIENT_ID / "
                     "_CLIENT_SECRET are not all set — not offering it on the "
                     "login screen.", self.name, self._prefix)
            return False
        return True

    def start(self, redirect_uri: str, state: str) -> str:
        doc = _discover(self._env("ISSUER"))
        params = {
            "response_type": "code",
            "client_id": self._env("CLIENT_ID"),
            "scope": self._env("SCOPES", "openid email profile"),
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": _derive(state, "nonce"),
            "code_challenge": _s256(_derive(state, "pkce")),
            "code_challenge_method": "S256",
        }
        return f"{doc['authorization_endpoint']}?{urllib.parse.urlencode(params)}"

    def exchange(self, code: str, redirect_uri: str, state: str = "") -> Identity:
        import httpx

        issuer = self._env("ISSUER")
        doc = _discover(issuer)
        client_id = self._env("CLIENT_ID")
        try:
            resp = httpx.post(
                doc["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "client_secret": self._env("CLIENT_SECRET"),
                    "code_verifier": _derive(state, "pkce"),
                },
                headers={"Accept": "application/json"},
                timeout=10,
            )
        except Exception as e:
            raise AuthError("token_exchange_failed", str(e)) from e
        if resp.status_code != 200:
            # The body carries the provider's own error code and can carry the
            # request back with it; log the code only, never the response.
            try:
                err = str(resp.json().get("error"))
            except Exception:
                err = f"HTTP {resp.status_code}"
            raise AuthError("token_exchange_failed", err)
        payload = resp.json()
        id_token = payload.get("id_token")
        if not id_token:
            raise AuthError("no_id_token",
                            "the provider returned no id_token — is the "
                            "'openid' scope granted?")

        algs = [a for a in (doc.get("id_token_signing_alg_values_supported")
                            or ["RS256"]) if a in _SAFE_ALGS]
        if not algs:
            raise AuthError("no_safe_algorithm",
                            "provider signs id_tokens with none of "
                            + ", ".join(_SAFE_ALGS))
        jwks_uri = doc["jwks_uri"]
        client = _jwks_clients.get(jwks_uri)
        if client is None:
            client = _jwks_clients[jwks_uri] = jwt.PyJWKClient(jwks_uri)
        try:
            key = client.get_signing_key_from_jwt(id_token).key
            claims = jwt.decode(id_token, key, algorithms=algs,
                                audience=client_id, issuer=doc["issuer"])
        except jwt.PyJWTError as e:
            raise AuthError("bad_id_token", str(e)) from e

        # Replay binding: the nonce we asked for must be the nonce that came
        # back. Cheap, and it is the check that makes a stolen id_token from
        # another login attempt useless here.
        if not hmac.compare_digest(str(claims.get("nonce") or ""),
                                   _derive(state, "nonce")):
            raise AuthError("bad_nonce", "id_token nonce does not match")

        return self._identity_from_claims(claims)

    def _identity_from_claims(self, claims: dict) -> Identity:
        """Verified claims -> the principal they are allowed to act as.

        Split out from `exchange` because this half is where the security
        decisions live and it needs no network to exercise: everything above
        proves the token is genuinely the provider's, everything here decides
        whether that buys any standing in QueryHub.
        """
        email = (claims.get("email") or "").strip().lower()
        if not email:
            raise AuthError("no_email",
                            "the provider returned no email — is the 'email' "
                            "scope granted?")
        # An unverified address is not an identity. The email IS the join to
        # this person's grants, so a provider that lets a user type any
        # address would let them type a colleague's.
        if claims.get("email_verified") is False:
            raise AuthError("email_unverified", "provider reports email unverified")
        domain = (cfg.get_setting("web_allowed_email_domain", "") or "").strip()
        if domain and not email.endswith("@" + domain.lower()):
            raise AuthError("email_domain", f"email not in @{domain}")

        from .. import admins, requesters
        row = requesters.by_email(email) or admins.by_email(email)
        if row is None:
            # Deliberately not "unknown user": the address may well be known
            # to the company and simply have no QueryHub standing.
            raise AuthError("not_onboarded",
                            "no QueryHub account is registered to that address")
        return Identity(
            principal_id=row["slack_user_id"],
            email=email,
            name=(claims.get("name") or claims.get("preferred_username")
                  or row.get("name")),
            provider=self.name,
            avatar=claims.get("picture"),
        )


def _configured_oidc() -> dict[str, OIDCProvider]:
    """Every OIDC provider the environment describes, in a stable order."""
    out: dict[str, OIDCProvider] = {}
    for key in sorted(os.environ):
        m = _OIDC_ENV_RE.match(key)
        if not m:
            continue
        token = m.group(1)
        pid = token.lower()
        if not _OIDC_ID_RE.match(pid):
            log.warning("ignoring %s: %r is not a usable provider id", key, pid)
            continue
        if pid in _RESERVED_IDS:
            log.warning("ignoring %s: %r is a built-in provider id", key, pid)
            continue
        out[pid] = OIDCProvider(pid, f"OIDC_{token}")
    return out


def oidc_ids() -> list[str]:
    """Ids of the configured OIDC providers — for surfaces that need to
    describe a provider (the audit vocabulary) rather than perform one."""
    return sorted(_configured_oidc())


# ---- registry ---------------------------------------------------------------

# The BUILT-IN providers. Kept as a plain dict because it is also the list
# other modules assert against (every built-in needs an audit label); the
# operator-configured OIDC providers are merged in below and cannot be
# enumerated at import time.
_ALL = {
    SlackOIDC.name: SlackOIDC(),
    LocalPassword.name: LocalPassword(),
}


def _registry() -> dict[str, object]:
    reg: dict[str, object] = dict(_ALL)
    reg.update(_configured_oidc())
    return reg


def enabled_providers() -> dict[str, object]:
    return {name: p for name, p in _registry().items() if p.enabled()}


def get_provider(name: str):
    p = _registry().get(name)
    if p is None or not p.enabled():
        return None
    return p
