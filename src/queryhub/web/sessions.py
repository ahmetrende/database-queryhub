"""Web session layer: short stateless access JWTs + DB-backed refresh
tokens (web_sessions, migration 061) + instant revocation.

Design per the handoff's AUTH.md:
  - access JWT is short (default 20 min) and verified on every request
    with signature + exp + a cheap revocation lookup;
  - the opaque refresh token (httpOnly cookie) is the re-verification
    checkpoint — rotating it re-checks Slack employment + whitelist;
  - revoking = flipping web_sessions.revoked_at; takes effect on the
    next request.

The signing secret is derived from the bot's master key (HMAC with a
fixed context label), so no new secret needs managing; set
WEB_SESSION_SECRET to override.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets as pysecrets
from datetime import datetime, timedelta, timezone

import jwt

from .. import db
from .. import config as cfg

log = logging.getLogger(__name__)

_ALG = "HS256"


def _signing_secret() -> bytes:
    override = os.environ.get("WEB_SESSION_SECRET")
    if override:
        return override.encode()
    master = cfg.ENV.master_key_path.read_bytes().strip()
    return hmac.new(master, b"queryhub-web-session-v1", hashlib.sha256).digest()


def access_ttl_minutes() -> int:
    return cfg.get_int("web_access_token_minutes", 20)


def refresh_ttl_hours() -> int:
    return cfg.get_int("web_refresh_token_hours", 12)


def _refresh_grace_seconds() -> int:
    """How long a just-superseded refresh token still works.

    Single-use refresh tokens with reuse detection are the right design, but
    without a grace window the ordinary race — two tabs refreshing together, or
    a retry after a lost response — is indistinguishable from a replay, and the
    theft response (revoke the session) fires on legitimate use. A few seconds
    is enough to absorb that while keeping a stolen, long-rotated token
    detectable. Set to 0 to restore strict single-use."""
    return cfg.get_int("web_refresh_grace_seconds", 30)


# ---- access JWT ------------------------------------------------------------

def mint_access(identity: dict, session_id: int) -> str:
    """identity: {slack_user_id, name, email, provider, avatar}."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": identity["slack_user_id"],
            "sid": session_id,
            "name": identity.get("name"),
            "email": identity.get("email"),
            "avatar": identity.get("avatar"),
            "provider": identity.get("provider", "slack"),
            "iat": now,
            "exp": now + timedelta(minutes=access_ttl_minutes()),
        },
        _signing_secret(),
        algorithm=_ALG,
    )


def verify_access(token: str) -> dict | None:
    """Signature + exp only — the caller does the revocation lookup."""
    try:
        return jwt.decode(token, _signing_secret(), algorithms=[_ALG])
    except jwt.InvalidTokenError:
        return None


# ---- refresh tokens (opaque, hashed at rest) -------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(principal_id: str, *, provider: str,
                   user_agent: str | None,
                   avatar_url: str | None = None) -> tuple[int, str]:
    """New login → one web_sessions row. Returns (session_id, refresh_token)."""
    token = pysecrets.token_urlsafe(48)
    row = db.insert_returning(
        "INSERT INTO web_sessions "
        " (slack_user_id, refresh_hash, auth_provider, user_agent, avatar_url, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW() + make_interval(hours => %s)) "
        "RETURNING id",
        (principal_id, _hash(token), provider, (user_agent or "")[:300],
         avatar_url, refresh_ttl_hours()),
    )
    return row["id"], token


def session_alive(session_id: int, principal_id: str | None = None) -> bool:
    """The per-request revocation check: one indexed lookup.

    `principal_id` BINDS the session row to the identity claiming it. The access
    token carries both `sub` and `sid`, and until this existed nothing checked
    that the two belonged together — a token asserting `sub` = the operator while
    riding another row's `sid` would have passed, and every authorization
    decision downstream (is_admin, is_super_admin, grant lookups) is keyed on
    `sub`.

    Forging such a token needs the signing secret, so this was not reachable on
    its own. It is here to remove the category rather than leave the secret as
    the only thing in the way: identity now has to agree with itself in two
    independent places, one of them a row this process did not mint.

    Omitting `principal_id` keeps the original revocation-only behaviour.
    """
    if principal_id is None:
        row = db.fetch_one(
            "SELECT 1 AS ok FROM web_sessions "
            "WHERE id = %s AND revoked_at IS NULL AND expires_at > NOW()",
            (session_id,),
        )
        return row is not None
    row = db.fetch_one(
        "SELECT 1 AS ok FROM web_sessions "
        "WHERE id = %s AND slack_user_id = %s "
        "  AND revoked_at IS NULL AND expires_at > NOW()",
        (session_id, principal_id),
    )
    return row is not None


def rotate_refresh(refresh_token: str) -> dict | None:
    """Validate + rotate a refresh token. Single-use with reuse detection.

    Returns:
      - the session row (id, slack_user_id, auth_provider) + a fresh
        `refresh_token` on success;
      - {"reuse": True} if the presented token is a REPLAY of a
        just-superseded token on a still-live session — the session is
        revoked as a suspected theft (see migration 062);
      - None if the token is unknown / revoked / expired.
    """
    old_hash = _hash(refresh_token)
    new_token = pysecrets.token_urlsafe(48)
    with db.transaction() as cur:
        # 1) Normal path: the token is the session's CURRENT refresh hash.
        cur.execute(
            "UPDATE web_sessions "
            "   SET refresh_hash = %s, prev_refresh_hash = %s, last_refresh_at = NOW() "
            " WHERE refresh_hash = %s AND revoked_at IS NULL AND expires_at > NOW() "
            "RETURNING id, slack_user_id, auth_provider, avatar_url",
            (_hash(new_token), old_hash, old_hash),
        )
        row = cur.fetchone()
        if row is not None:
            row = dict(row)
            row["refresh_token"] = new_token
            return row
        # 2) Grace window: the token matches the SUPERSEDED hash, but the
        #    rotation that superseded it happened seconds ago. That is not
        #    theft — it is the ordinary race: two browser tabs refreshing at
        #    once, or a client retrying after a lost response. Treating it as
        #    theft revoked the session, so opening a second tab could sign the
        #    user out everywhere. Rotate again instead, keeping `old_hash` as
        #    prev so the other tab's in-flight retry also lands in the window.
        grace = _refresh_grace_seconds()
        if grace > 0:
            cur.execute(
                "UPDATE web_sessions "
                "   SET refresh_hash = %s, prev_refresh_hash = %s, "
                "       last_refresh_at = NOW() "
                " WHERE prev_refresh_hash = %s AND revoked_at IS NULL "
                "   AND expires_at > NOW() "
                "   AND last_refresh_at > NOW() - make_interval(secs => %s) "
                "RETURNING id, slack_user_id, auth_provider, avatar_url",
                (_hash(new_token), old_hash, old_hash, grace),
            )
            row = cur.fetchone()
            if row is not None:
                row = dict(row)
                row["refresh_token"] = new_token
                return row
        # 3) Reuse detection: a superseded hash on a still-live session, OUTSIDE
        #    the grace window → replay of a long-rotated token → revoke the
        #    whole session (theft response).
        cur.execute(
            "UPDATE web_sessions "
            "   SET revoked_at = NOW(), revoked_reason = 'refresh token reuse detected' "
            " WHERE prev_refresh_hash = %s AND revoked_at IS NULL "
            "RETURNING id, slack_user_id",
            (old_hash,),
        )
        reused = cur.fetchone()
    if reused is not None:
        log.warning("web session %s revoked: refresh-token reuse detected (user %s)",
                    reused["id"], reused["slack_user_id"])
        return {"reuse": True}
    return None


def revoke_by_refresh(refresh_token: str, reason: str) -> bool:
    """Revoke the session identified by a refresh token. Used at sign-out:
    the access JWT may already be expired (so verify_access yields nothing),
    but the refresh cookie is long-lived and always present — logging out
    must kill the server-side session, not just clear browser cookies."""
    with db.transaction() as cur:
        cur.execute(
            "UPDATE web_sessions SET revoked_at = NOW(), revoked_reason = %s "
            "WHERE refresh_hash = %s AND revoked_at IS NULL",
            (reason, _hash(refresh_token)),
        )
        return cur.rowcount > 0


def revoke_session(session_id: int, reason: str) -> None:
    db.execute(
        "UPDATE web_sessions SET revoked_at = NOW(), revoked_reason = %s "
        "WHERE id = %s AND revoked_at IS NULL",
        (reason, session_id),
    )


def revoke_user(principal_id: str, reason: str) -> int:
    """Instant kill switch for one user's web access (all sessions)."""
    with db.transaction() as cur:
        cur.execute(
            "UPDATE web_sessions SET revoked_at = NOW(), revoked_reason = %s "
            "WHERE slack_user_id = %s AND revoked_at IS NULL",
            (reason, principal_id),
        )
        return cur.rowcount
