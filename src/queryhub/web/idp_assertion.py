"""Verification of identity assertions from the IDP panel (AUTH.md section 3).

The panel is a BFF: the browser never reaches this service, so a proxied
request carries no cookie. It carries instead a 60-second Ed25519 JWT naming
the VERIFIED corporate address of the human behind it.

That address is resolved the same way an OIDC login resolves one, through
`requesters.by_email` / `admins.by_email`, so this is a third way to prove an
existing identity and never a new authorization subject. The assertion says
who, and nothing about what; authority is read from this service's own tables
afterwards, unchanged.

The assertion is bound to method + path + body, so a captured token cannot be
replayed against a different route or a different statement, and its `jti` is
single-use.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import jwt as pyjwt

from .. import admins
from .. import config as cfg
from .. import db, requesters

log = logging.getLogger(__name__)

_ALG = "EdDSA"
_REPLAY_WINDOW = timedelta(minutes=2)


class AssertionError_(Exception):
    """Assertion refused. `code` is a short, log-safe reason."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def canonical_string(method: str, path: str, body: bytes) -> str:
    return f"{method.upper()}\n{path}\n{body.decode('utf-8', 'replace')}"


def body_hash(method: str, path: str, body: bytes) -> str:
    return hashlib.sha256(
        canonical_string(method, path, body).encode("utf-8")).hexdigest()


def _enabled() -> bool:
    return (cfg.get_setting("idp_assertion_enabled", "off") or "").strip().lower() \
        in {"on", "1", "true", "yes"}


def _public_key_for(kid: str) -> str | None:
    raw = cfg.get_setting("idp_public_keys", "{}") or "{}"
    try:
        keys = json.loads(raw)
    except ValueError:
        log.error("idp_public_keys is not valid JSON; refusing all assertions")
        return None
    key = keys.get(kid)
    return key if isinstance(key, str) and key.strip() else None


def _claim_jti(jti: str, expires_at: datetime) -> bool:
    """True if this jti was unused. The PRIMARY KEY decides, so a concurrent
    duplicate loses the insert rather than racing a SELECT."""
    row = db.fetch_one(
        "INSERT INTO idp_assertion_jti (jti, expires_at) VALUES (%s, %s) "
        "ON CONFLICT (jti) DO NOTHING RETURNING jti",
        (jti, expires_at),
    )
    if row is None:
        return False
    db.execute("DELETE FROM idp_assertion_jti WHERE expires_at < NOW()")
    return True


class Principal(NamedTuple):
    """Who the assertion resolved to, read from QueryHub's own tables.

    `name` comes from the `requesters`/`admins` row rather than from the
    token's `name` claim, on the same principle as everything else here: the
    panel asserts who is acting, and this service reads what it knows about
    them. Without it, `claims.get("name") or uid` — 41 call sites — renders
    every IdP user as a raw principal id in DMs, the audit feed and the queue.
    """

    id: str
    name: str | None


def verify(token: str, method: str, path: str, body: bytes) -> Principal:
    """Return the principal for this request, or raise AssertionError_."""
    if not _enabled():
        raise AssertionError_("disabled", "IDP assertions are not enabled.")

    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.PyJWTError as e:
        raise AssertionError_("malformed", str(e)) from e

    # The algorithm is pinned from OUR configuration and never read from the
    # token header: doing otherwise is how `alg: none` and an HS256 token
    # signed with the published public key get in.
    kid = header.get("kid") or ""
    public_key = _public_key_for(kid)
    if public_key is None:
        raise AssertionError_("unknown_kid", f"No public key for kid {kid!r}.")

    try:
        claims = pyjwt.decode(
            token, public_key, algorithms=[_ALG],
            audience=cfg.get_setting("idp_audience", "queryhub"),
            issuer=cfg.get_setting("idp_issuer", "idp"),
            options={"require": ["exp", "iat", "sub", "jti", "aud", "iss"]},
        )
    except pyjwt.ExpiredSignatureError as e:
        raise AssertionError_("expired", str(e)) from e
    except pyjwt.InvalidIssuerError as e:
        raise AssertionError_("bad_issuer", str(e)) from e
    except pyjwt.InvalidAudienceError as e:
        raise AssertionError_("bad_audience", str(e)) from e
    except pyjwt.PyJWTError as e:
        raise AssertionError_("bad_signature", str(e)) from e

    if claims.get("bh") != body_hash(method, path, body):
        raise AssertionError_("body_mismatch",
                              "Assertion is not bound to this request.")

    if not _claim_jti(claims["jti"],
                      datetime.now(timezone.utc) + _REPLAY_WINDOW):
        raise AssertionError_("replayed", "Assertion has already been used.")

    email = str(claims["sub"]).strip().lower()
    if not email:
        raise AssertionError_("malformed", "Empty sub claim.")

    # The same domain gate the OIDC providers apply. The panel checks it too,
    # but this is the check that matters: it is the one an attacker who is
    # already past the panel cannot skip.
    domain = (cfg.get_setting("web_allowed_email_domain", "") or "").strip()
    if domain and not email.endswith("@" + domain.lower()):
        raise AssertionError_("bad_domain", "Address is outside the allowed domain.")

    row = requesters.by_email(email) or admins.by_email(email)
    if row is None:
        # Deliberately not "unknown user": the address may be perfectly well
        # known to the company and simply have no QueryHub standing. Ambiguity
        # (two rows, one address) also lands here, because by_email returns
        # None for it, and refusing is the only safe answer.
        raise AssertionError_("not_onboarded",
                              "No QueryHub account is registered to that address.")
    return Principal(row["slack_user_id"], row.get("name"))
