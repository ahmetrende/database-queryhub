"""Request-scoped dependencies: the verify_session middleware (as a
FastAPI dependency so no route can forget it) + the live Slack
employment check used at the dangerous moments."""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request
from starlette.requests import HTTPConnection

from .. import admins, db, requesters
from .. import config as cfg
from . import idp_assertion, sessions

log = logging.getLogger(__name__)

SESSION_COOKIE = "qh_session"
REFRESH_COOKIE = "qh_refresh"
ASSERTION_HEADER = "x-idp-assertion"


def _error(status: int, code: str, message: str,
           **extra) -> HTTPException:
    """API_CONTRACT error envelope.

    `extra` adds structured fields beside code/message for the errors that
    have them — `reasons` on a confirmation-required 409. Keyword-only and
    additive, so every existing call site produces a byte-identical body.
    """
    detail = {"code": code, "message": message}
    detail.update({k: v for k, v in extra.items() if v not in (None, (), [])})
    return HTTPException(status_code=status, detail=detail)


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
    # A request carrying an assertion comes from the IDP panel, and is judged
    # ONLY by that assertion. Checked first and exclusively: falling through to
    # the cookie after refusing one would let a stale browser session stand in
    # for a proxy call this service just rejected.
    raw = conn.headers.get(ASSERTION_HEADER)
    if raw:
        try:
            principal = idp_assertion.verify(
                raw,
                conn.scope.get("method", "GET"),
                _request_target(conn),
                conn.scope.get("_body", b""),
            )
        except idp_assertion.AssertionError_ as e:
            log.warning("idp assertion refused: %s", e.code)
            raise _error(401, "unauthenticated",
                         "Invalid identity assertion.") from e
        # No `sid`: a proxied request has no server-side session to revoke.
        # Its bound is the 60-second lifetime plus the panel's own revocation,
        # so the liveness lookup below must not run for it — which the early
        # return here is what guarantees.
        # `name` is carried so the 41 `claims.get("name") or uid` call sites
        # render a person rather than a raw principal id. It comes from the
        # requesters/admins row, not from the token's `name` claim: the panel
        # says who is acting, this service says what it knows about them.
        return {"sub": principal.id, "name": principal.name,
                "provider": "idp", "sid": None}

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
    if not sessions.session_alive(claims["sid"], claims.get("sub")):
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


def _request_target(conn: HTTPConnection) -> str:
    """Path plus query string — exactly the target the panel signed. Verifying
    a bare path would accept an assertion minted for a different query."""
    path = conn.scope.get("path", "")
    qs = conn.scope.get("query_string", b"").decode()
    return f"{path}?{qs}" if qs else path


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


# users.info answers that settle it: the account is gone, or this workspace
# cannot see it. `users_not_found` is lookupByEmail's spelling; users.info says
# `user_not_found`, which the old substring test missed. Anything else, such as
# a timeout, a rate limit or a bad bot token, says nothing about the person.
_GONE_ERRORS = frozenset({"user_not_found", "users_not_found", "user_not_visible"})


def slack_employment(principal_id: str) -> str:
    """Ask Slack whether `principal_id` still works here: "active", "gone", or
    "unknown" when Slack could not answer.

    With no Slack workspace configured there is nobody to ask, and the answer
    is "active": the enabled requester or admin row is the whole check. Every
    "active" answer is recorded (`slack_liveness`), so a later "unknown" can be
    judged by how recently Slack last vouched for the person."""
    if not cfg.ENV.slack_enabled:
        return "active"
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        log.error("SLACK_BOT_TOKEN is set but slack_sdk is not installed; "
                  "employment cannot be checked")
        return "unknown"
    try:
        info = WebClient(token=cfg.ENV.slack_bot_token, timeout=5).users_info(
            user=principal_id)
    except SlackApiError as e:
        code = (getattr(e, "response", None) or {}).get("error")
        if code in _GONE_ERRORS:
            return "gone"
        log.warning("users.info for %s answered %s", principal_id, code)
        return "unknown"
    except Exception as e:  # noqa: BLE001 -- transport: nothing about the person
        log.warning("users.info transport failure for %s: %s", principal_id,
                    type(e).__name__)
        return "unknown"
    if (info.get("user") or {}).get("deleted"):
        return "gone"
    try:
        db.execute(
            "INSERT INTO slack_liveness (principal_id, active_at) VALUES (%s, NOW()) "
            "ON CONFLICT (principal_id) DO UPDATE SET active_at = EXCLUDED.active_at",
            (principal_id,))
    except Exception:  # noqa: BLE001 -- a bookkeeping write never refuses anyone
        log.warning("could not record the users.info answer", exc_info=True)
    return "active"


def _vouched_recently(principal_id: str) -> bool:
    hours = cfg.get_int("web_employment_grace_hours", 2)
    try:
        return db.fetch_one(
            "SELECT 1 AS ok FROM slack_liveness WHERE principal_id = %s "
            "   AND active_at > NOW() - make_interval(hours => %s)",
            (principal_id, hours)) is not None
    except Exception:  # noqa: BLE001
        log.warning("could not read slack_liveness", exc_info=True)
        return False


def employment_verdict(principal_id: str) -> str:
    """The AUTH.md 'dangerous moment' check at sign-in and refresh, whichever
    provider did the login: "active", "gone", or "unconfirmed".

    When Slack cannot answer, the person passes only if Slack called them
    active within `web_employment_grace_hours`. A transport error used to pass
    everyone, so during a Slack outage an offboarded person whose rows had not
    been removed yet kept refreshing, for as long as the outage lasted. A write
    asks `slack_employment` itself and does not accept an old answer."""
    state = slack_employment(principal_id)
    if state == "unknown":
        return "active" if _vouched_recently(principal_id) else "unconfirmed"
    return state


def employment_checked(claims: dict) -> bool:
    """True when this session's principal is a Slack person to ask about.

    A `local` account has no Slack identity; its liveness is its own row. Any
    other provider (Slack, company SSO, the IdP) resolves to a Slack principal,
    so a test on `provider == "slack"` skipped the check for every sign-in that
    came through SSO."""
    provider = claims.get("provider")
    return bool(provider) and provider != "local"
