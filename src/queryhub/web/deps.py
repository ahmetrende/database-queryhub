"""Request-scoped dependencies: the verify_session middleware (as a
FastAPI dependency so no route can forget it) + the live Slack
employment check used at the dangerous moments."""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request
from starlette.requests import HTTPConnection

from .. import admins, requesters
from .. import config as cfg
from . import sessions

log = logging.getLogger(__name__)

SESSION_COOKIE = "qh_session"
REFRESH_COOKIE = "qh_refresh"


def _error(status: int, code: str, message: str) -> HTTPException:
    """API_CONTRACT error envelope."""
    return HTTPException(status_code=status,
                         detail={"code": code, "message": message})


def current_user(conn: HTTPConnection) -> dict:
    """AUTH.md §3 — runs on every protected route, cheap:
    token extract → signature+exp → revocation lookup. Returns the
    session claims; endpoints do their own per-query grant checks.

    Typed as `HTTPConnection` — the base class of both Request and WebSocket —
    rather than `Request`, and that is a fix rather than a tidy-up. The queries
    router carries `Depends(block_pw_gate)`, which depends on this; FastAPI
    cannot supply a `Request` to a WebSocket route, so every handshake on
    /api/queries/{id}/stream raised
    `TypeError: current_user() missing 1 required positional argument: 'request'`
    and was rejected with a 500. The live-status stream had therefore never
    worked since that router dependency was added — and because the frontend
    falls back to HTTP polling when the socket will not open, the feature
    degraded silently instead of failing visibly.

    Cookies and headers are on HTTPConnection, so nothing else changes.
    """
    token = conn.cookies.get(SESSION_COOKIE)
    if not token:
        auth = conn.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise _error(401, "unauthenticated", "No session.")
    claims = sessions.verify_access(token)
    if claims is None:
        raise _error(401, "unauthenticated", "Session expired or invalid.")
    if not sessions.session_alive(claims["sid"]):
        raise _error(401, "unauthenticated", "Session revoked.")
    # Liveness for local accounts: a disabled local_users row must
    # lock the account out on the very next request, without waiting for the
    # short access token to expire or an explicit session revoke. Slack /
    # requester / admin liveness is covered by require_whitelisted +
    # require_admin (both check the enabled row) on the routes that gate on
    # them; local accounts have no such external check, so enforce it here.
    if claims.get("provider") == "local":
        from .. import local_users
        uname = local_users.username_of(claims.get("sub") or "")
        row = local_users.get(uname) if uname else None
        if row is None or not row.get("enabled", False):
            raise _error(401, "unauthenticated", "Account disabled.")
    return claims


def require_whitelisted(claims: dict) -> None:
    """The same gate /sql applies at entry: an enabled requesters row
    (admins pass implicitly)."""
    uid = claims["sub"]
    if admins.is_admin(uid) or requesters.is_allowed(uid):
        return
    raise _error(403, "forbidden",
                 "You are not whitelisted for QueryHub. Ask the DBA team.")


def block_if_password_change_required(claims: dict) -> None:
    """403 if this is a local account flagged must_change_pw. Called before
    the dangerous action (query submit) so a handed-off account can't be used
    until its password is reset. Live single-row lookup; no-op for non-local
    providers."""
    if claims.get("provider") != "local":
        return
    from .. import local_users
    uname = local_users.username_of(claims.get("sub") or "")
    if not uname:
        return
    row = local_users.get(uname)
    if row and row.get("must_change_pw"):
        raise _error(403, "password_change_required",
                     "You must change your password before running queries.")


def origin_is_same_site(conn: HTTPConnection) -> bool:
    """True when the request's Origin matches its own Host (or there is none).

    Shared by the HTTP security middleware and the WebSocket handshake so the
    two cannot drift. Compared against the request's own Host rather than a
    configured value, so a legitimate same-origin call is never blocked whatever
    hostname the deployment answers on.

    A MISSING Origin passes: browsers always send one on a cross-site request,
    so its absence means a non-browser client — which carries no ambient
    credential, and still has to authenticate.
    """
    origin = conn.headers.get("origin")
    if not origin:
        return True
    from urllib.parse import urlsplit
    o_host = urlsplit(origin).netloc.lower()
    req_host = (conn.headers.get("host") or "").lower()
    if not o_host or not req_host:
        return True
    return o_host == req_host


def _trust_proxy() -> bool:
    val = (cfg.get_setting("web_trusted_proxy", "off") or "").strip().lower()
    return val in {"on", "1", "true", "yes"}


def _trusted_proxy_hops() -> int:
    """How many proxy hops sit in front — i.e. how far from the RIGHT to read."""
    try:
        n = cfg.get_int("web_trusted_proxy_hops", 1)
    except Exception:
        return 1
    return n if 1 <= n <= 10 else 1


def client_ip(request: Request) -> str | None:
    """Best-effort client IP for the login throttle and the audit trail.

    X-Forwarded-For is CLIENT-controlled unless a trusted reverse proxy sets
    it, so honor it only when `web_trusted_proxy` is enabled.
    Otherwise a client could spoof the header to bypass the per-IP login
    throttle or poison the audit trail — default to the real peer address.

    WHICH hop matters as much as whether to read the header, and taking the
    leftmost was wrong. nginx's standard
    `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for` APPENDS the
    real peer to whatever the client sent, so a request carrying
    `X-Forwarded-For: 9.9.9.9` arrives as `9.9.9.9, <real-ip>` — and the
    leftmost entry is the attacker's own string. The throttle then counts per
    attacker-chosen key, which makes the per-IP login limit trivially evadable,
    and the same forged value lands in audit_log.

    The trustworthy end is the RIGHT: every proxy appends, so the last entry was
    written by the hop nearest us. Step back `web_trusted_proxy_hops` entries
    (default 1 = one reverse proxy in front; set 2 for proxy-behind-proxy).
    """
    if _trust_proxy():
        parts = [p.strip() for p in
                 request.headers.get("x-forwarded-for", "").split(",")
                 if p.strip()]
        if parts:
            hops = _trusted_proxy_hops()
            # A chain shorter than the configured hop count means the header
            # does not describe the deployment. Degrade to the leftmost rather
            # than index out of range: a mis-set number should not 500 every
            # request.
            return parts[-hops] if len(parts) >= hops else parts[0]
    return request.client.host if request.client else None


def block_pw_gate(claims: dict = Depends(current_user)) -> None:
    """Router-level guard: a local account flagged must_change_pw is
    blocked from every action route — data, queries, admin, requests — until
    it resets its password. Applied as a router dependency so no route can
    forget it; /me, logout and the change-password endpoint live on the app
    (not these routers) and stay reachable so the reset flow works. FastAPI
    caches current_user within a request, so this does not re-verify."""
    block_if_password_change_required(claims)


def slack_employment_ok(principal_id: str) -> bool:
    """Live users.info — the AUTH.md 'dangerous moment' check. Runs at
    refresh time and right before RW/DDL submits, independent of which
    provider did the login. Fail-closed on a definitive bad answer,
    fail-open on transport errors (Slack hiccups must not brick the app;
    the short session TTL still bounds the window)."""
    try:
        from slack_sdk import WebClient
        info = WebClient(token=cfg.ENV.slack_bot_token).users_info(
            user=principal_id)
        u = info["user"]
        if u.get("deleted"):
            return False
        return True
    except Exception as e:
        # users_not_found / user_not_visible = definitive → fail closed.
        msg = str(e)
        if "users_not_found" in msg or "user_not_visible" in msg:
            return False
        log.warning("users.info transport failure for %s: %s", principal_id, e)
        return True
