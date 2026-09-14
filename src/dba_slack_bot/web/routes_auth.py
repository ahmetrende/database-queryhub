"""Authentication HTTP surface: provider discovery, login, OAuth
round-trip, password change, refresh, sign-out.

These seven routes used to be defined inside `create_app()`, which made that
function 537 lines and — more to the point — made the entire auth surface
invisible to anyone navigating the code by router: `grep -rn "include_router"`
listed data, queries, requests and admin, and gave no hint that login lived
somewhere else entirely. They are a router like every other group now.

The session-cookie helpers moved with them, since nothing outside auth used
them. `create_app()` keeps only what is genuinely app-wide: middleware,
lifespan, exception handlers, health probes, /api/me, /api/changelog and the
static mount.
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .. import audit
from .. import config as cfg
from . import auth_providers, deps, sessions

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth")

OAUTH_STATE_COOKIE = "qh_oauth_state"

def base_url() -> str:
    """Externally visible origin (the tunnel makes this localhost in dev).

    WEB_BASE_URL (env) wins over bot_config.web_base_url: bot_config is shared
    by every instance on the same bot DB, so a second instance serving the same
    fleet from another port (e.g. an alternate-theme deployment) needs a
    per-process override for its OAuth redirect + links."""
    env = (os.environ.get("WEB_BASE_URL") or "").strip()
    if env:
        return env.rstrip("/")
    return (cfg.get_setting("web_base_url", "http://localhost:8080") or "").rstrip("/")

def _cookie_secure() -> bool:
    """Whether session cookies get the `Secure` flag.

    Derived from the deployment when the operator has not decided, because the
    old fail-open default was reachable by following the documented install:
    scripts/install.sh generates a certificate, sets WEB_SSL_CERTFILE, prints
    "open https://localhost:8080" — and never touches `web_cookie_secure`. So
    the recommended path produced an HTTPS deployment whose session cookies
    could be sent over plain HTTP. CONFIGURATION.md said to turn it on; nothing
    enforced or even warned, and an operator installing a tool does not read
    every config row first.

    Now: an explicit `web_cookie_secure` always wins, in both directions — an
    operator who says `off` behind a TLS-terminating proxy gets `off`. With the
    key unset, `https` in web_base_url means Secure. That is fail-safe rather
    than fail-open, and it costs a localhost-HTTP developer nothing, since their
    base URL is http.
    """
    raw = (cfg.get_setting("web_cookie_secure", "") or "").strip().lower()
    if raw in {"on", "1", "true", "yes"}:
        return True
    if raw in {"off", "0", "false", "no"}:
        return False
    return base_url().lower().startswith("https://")

def _must_change_password(claims: dict) -> bool:
    """True only for a local account flagged must_change_pw."""
    if claims.get("provider") != "local":
        return False
    from .. import local_users
    uname = local_users.username_of(claims.get("sub") or "")
    row = local_users.get(uname) if uname else None
    return bool(row and row.get("must_change_pw"))

def _set_session_cookies(resp: Response, access: str, refresh: str) -> None:
    resp.set_cookie(
        deps.SESSION_COOKIE, access,
        max_age=sessions.access_ttl_minutes() * 60,
        httponly=True, samesite="lax", secure=_cookie_secure(), path="/",
    )
    resp.set_cookie(
        deps.REFRESH_COOKIE, refresh,
        max_age=sessions.refresh_ttl_hours() * 3600,
        httponly=True, samesite="lax", secure=_cookie_secure(),
        path="/api/auth",
    )


@router.get("/providers")
def providers():
    # `kind` tells the login screen how to render each provider: "oauth" =
    # a redirect button (/api/auth/<id>/start), "password" = a
    # username/password form (POST /api/auth/local/login).
    #
    # orgLabel rides along because the login screen needs the deployment's
    # name ("Restricted to the <org> workspace") and this is the only
    # pre-auth endpoint it can ask — /me requires a session. Without it the
    # sign-in page fell back to a hardcoded placeholder, so every install
    # advertised someone else's company.
    return {
        "orgLabel": cfg.get_setting("web_org_label", "QueryHub") or "QueryHub",
        "providers": [
            {"id": name, "label": p.label, "kind": getattr(p, "kind", "oauth")}
            for name, p in auth_providers.enabled_providers().items()
        ],
    }

@router.post("/local/login")
async def auth_local_login(request: Request):
    """Vanilla-profile login: verify a built-in local_users account and
    issue the same session (JWT + refresh) as any other provider. No
    Slack, no redirect. Enabled by web_auth_local_enabled."""
    p = auth_providers.get_provider("local")
    if p is None or getattr(p, "kind", "") != "password":
        raise deps._error(404, "not_found", "Local login is not enabled.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        raise deps._error(400, "bad_request",
                          "Username and password are required.")

    # Brute-force throttle: sliding-window failure caps per username and
    # per client IP (429 while saturated). Checked BEFORE the KDF so a
    # locked-out attacker can't burn CPU either.
    from . import login_throttle
    _ip = deps.client_ip(request) or "?"
    _ukey = f"u:{username.lower()}"
    _ipkey = f"ip:{_ip}"
    wait = login_throttle.retry_after_seconds(_ukey, _ipkey)
    if wait:
        raise deps._error(429, "rate_limited",
                          "Too many failed sign-in attempts. "
                          f"Try again in about {max(1, wait // 60)} minute(s).")
    try:
        ident = p.verify(username, password)
    except auth_providers.AuthError:
        login_throttle.record_failure(_ukey, _ipkey)
        # One opaque failure — never reveal whether the username exists.
        raise deps._error(401, "bad_credentials",
                          "Invalid username or password.")
    login_throttle.clear(_ukey)

    # Same entry gate as Slack: an enabled requester or an admin.
    from .. import admins, local_users, requesters
    if not (admins.is_admin(ident.principal_id)
            or requesters.is_allowed(ident.principal_id)):
        log.warning("local login rejected (not whitelisted): %s",
                    ident.principal_id)
        raise deps._error(403, "forbidden",
                          "This account is not authorized for QueryHub.")

    sid, refresh = sessions.create_session(
        ident.principal_id, provider=ident.provider,
        user_agent=request.headers.get("user-agent"), avatar_url=None)
    access = sessions.mint_access(
        {"slack_user_id": ident.principal_id, "name": ident.name,
         "email": ident.email, "provider": ident.provider,
         "avatar": None}, sid)
    try:
        local_users.touch_login(username)
    except Exception:
        log.warning("local_users.touch_login failed", exc_info=True)
    _ip = deps.client_ip(request)
    audit.log(None, ident.principal_id, ident.name, "web_login",
              {"provider": ident.provider, "session_id": sid, "ip": _ip,
               "user_agent": request.headers.get("user-agent")})
    resp = JSONResponse({"ok": True})
    _set_session_cookies(resp, access, refresh)
    return resp

@router.get("/{provider}/start")
def auth_start(provider: str):
    p = auth_providers.get_provider(provider)
    if p is None:
        raise deps._error(404, "not_found",
                          f"Login provider '{provider}' is not enabled.")
    redirect_uri = f"{base_url()}/api/auth/{provider}/callback"
    state = auth_providers.make_state()
    try:
        url = p.start(redirect_uri, state)
    except auth_providers.AuthError as e:
        raise deps._error(500, "server_error", str(e))
    resp = RedirectResponse(url, status_code=302)
    # Bind the OAuth state to THIS browser: a pre-auth nonce cookie the
    # callback must echo back. A server-signed state alone proves only
    # that we issued it, not that the same browser is completing the
    # flow — an attacker could paste their own valid callback URL to a
    # victim (login CSRF / session fixation). SameSite=Lax lets the
    # cookie ride the top-level redirect back from Slack.
    resp.set_cookie(OAUTH_STATE_COOKIE, state, max_age=600, httponly=True,
                    samesite="lax", secure=_cookie_secure(), path="/api/auth")
    return resp

@router.get("/{provider}/callback")
def auth_callback(provider: str, request: Request,
                  code: str = "", state: str = ""):
    p = auth_providers.get_provider(provider)
    if p is None:
        raise deps._error(404, "not_found",
                          f"Login provider '{provider}' is not enabled.")
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    state_ok = bool(code and state and cookie_state
                    and hmac.compare_digest(cookie_state, state)
                    and auth_providers.check_state(state))
    if not state_ok:
        bad = RedirectResponse(f"{base_url()}/?auth_error=bad_state", 302)
        bad.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth")
        return bad
    redirect_uri = f"{base_url()}/api/auth/{provider}/callback"
    try:
        # `state` goes through as well: a generic OIDC provider derives its
        # PKCE verifier and nonce from it, and both have to be recomputed
        # here to be checked. It is safe to pass — it was just verified
        # against the browser cookie and our own signature above.
        ident = p.exchange(code, redirect_uri, state)
    except auth_providers.AuthError as e:
        log.warning("login failed via %s: %s", provider, e.code)
        return RedirectResponse(f"{base_url()}/?auth_error={e.code}", 302)

    # Same entry gate as /sql: enabled requester or admin.
    from .. import admins, requesters
    if not (admins.is_admin(ident.principal_id)
            or requesters.is_allowed(ident.principal_id)):
        log.warning("login rejected (not whitelisted): %s", ident.principal_id)
        return RedirectResponse(f"{base_url()}/?auth_error=not_whitelisted", 302)

    # Still employed? This used to run only at refresh, which was enough while
    # every redirect login WAS a Slack login: Slack will not authenticate a
    # deactivated account, so the check could not fail here. An external SSO
    # breaks that assumption — the company IdP may still authenticate someone
    # Slack has deactivated, and the row that admits them stays enabled until a
    # human removes it. Without this, such a login succeeded and held a session
    # until its first refresh (web_access_token_minutes).
    #
    # Measured on live data 2026-08-14: one enabled requester is already
    # deactivated in Slack, so this is not a hypothetical ordering.
    #
    # `local` is exempt: its principals are `local:<username>` and have no
    # Slack identity to ask about. The check fails closed only on a definitive
    # answer, so a Slack outage does not block sign-in.
    if ident.provider != "local" and not deps.slack_employment_ok(
            ident.principal_id):
        log.warning("login rejected (Slack account gone): %s",
                    ident.principal_id)
        return RedirectResponse(f"{base_url()}/?auth_error=account_gone", 302)

    sid, refresh = sessions.create_session(
        ident.principal_id, provider=ident.provider,
        user_agent=request.headers.get("user-agent"),
        avatar_url=ident.avatar)
    access = sessions.mint_access(
        {"slack_user_id": ident.principal_id, "name": ident.name,
         "email": ident.email, "provider": ident.provider,
         "avatar": ident.avatar}, sid)
    _ip = deps.client_ip(request)
    audit.log(None, ident.principal_id, ident.name, "web_login",
              {"provider": ident.provider, "session_id": sid, "ip": _ip,
               "user_agent": request.headers.get("user-agent")})
    resp = RedirectResponse(f"{base_url()}/", status_code=302)
    _set_session_cookies(resp, access, refresh)
    resp.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth")
    return resp

@router.post("/local/change-password")
async def auth_local_change_password(
        request: Request, claims: dict = Depends(deps.current_user)):
    """Self-service password change for a local account. Verifies the
    current password, stores the new one (clearing must_change_pw), then
    revokes every session for the principal so all devices re-login with
    the new password."""
    if claims.get("provider") != "local":
        raise deps._error(400, "bad_request",
                          "Password change is only for local accounts.")
    from .. import local_users
    from . import login_throttle
    username = local_users.username_of(claims.get("sub") or "")
    if not username:
        raise deps._error(400, "bad_request", "Not a local account.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    current = body.get("currentPassword") or ""
    new = body.get("newPassword") or ""
    if not current or not new:
        raise deps._error(400, "bad_request",
                          "Current and new passwords are required.")
    # Throttle current-password guesses the same way login is throttled.
    tkey = f"pwchange:{username}"
    if login_throttle.retry_after_seconds(tkey):
        raise deps._error(429, "rate_limited",
                          "Too many attempts. Try again shortly.")
    err = local_users.change_password(username, current, new)
    if err == "bad_credentials":
        login_throttle.record_failure(tkey)
        raise deps._error(401, "bad_credentials",
                          "Current password is incorrect.")
    if err == "weak_password":
        raise deps._error(400, "weak_password",
                          f"New password must be at least "
                          f"{local_users.MIN_PASSWORD_LEN} characters.")
    if err == "same_password":
        raise deps._error(400, "same_password",
                          "New password must differ from the current one.")
    login_throttle.clear(tkey)
    n = sessions.revoke_user(claims["sub"], "local password changed")
    audit.log(None, claims["sub"], claims.get("name"),
              "local_password_changed", {"sessions_revoked": n})
    resp = JSONResponse({"ok": True, "reauth": True})
    resp.delete_cookie(deps.SESSION_COOKIE, path="/")
    resp.delete_cookie(deps.REFRESH_COOKIE, path="/api/auth")
    return resp

@router.post("/refresh")
def auth_refresh(request: Request):
    """AUTH.md §4 — the re-verification checkpoint: refresh token +
    live users.info + whitelist re-check, then a fresh short JWT."""
    token = request.cookies.get(deps.REFRESH_COOKIE)
    if not token:
        raise deps._error(401, "unauthenticated", "No refresh token.")
    rotated = sessions.rotate_refresh(token)
    if rotated is None:
        raise deps._error(401, "unauthenticated", "Session expired.")
    if rotated.get("reuse"):
        # A superseded refresh token was replayed → suspected theft;
        # rotate_refresh already revoked the session. Force re-login.
        raise deps._error(401, "unauthenticated",
                          "Session ended for security reasons. Please sign in again.")
    uid = rotated["slack_user_id"]
    # The live users.info "still employed?" check keys on a Slack id, so it
    # runs for every provider whose principal IS one — which is all of them
    # except `local`, whose ids are `local:<username>` and whose liveness
    # gate is the whitelist re-check below plus its own disabled flag.
    #
    # This used to test `== "slack"`, which was right while Slack was the
    # only redirect provider and silently wrong the moment a second one
    # existed: an external SSO login would have skipped the offboarding
    # check entirely and kept refreshing after the person left.
    if rotated["auth_provider"] != "local" and not deps.slack_employment_ok(uid):
        sessions.revoke_session(rotated["id"], "users.info: gone at refresh")
        raise deps._error(401, "unauthenticated", "Slack account gone.")
    from .. import admins, requesters
    if not (admins.is_admin(uid) or requesters.is_allowed(uid)):
        sessions.revoke_session(rotated["id"], "whitelist lost at refresh")
        raise deps._error(401, "unauthenticated", "Access removed.")
    prof = requesters.get(uid) or {}
    access = sessions.mint_access(
        {"slack_user_id": uid, "name": prof.get("name"),
         "email": prof.get("email"), "provider": rotated["auth_provider"],
         # Avatar rides the stable session row, so it survives rotation.
         "avatar": rotated.get("avatar_url")},
        rotated["id"])
    resp = JSONResponse({"ok": True})
    _set_session_cookies(resp, access, rotated["refresh_token"])
    return resp

@router.post("/signout", status_code=204)
def signout(request: Request):
    # Revoke the server-side session, not just the browser cookies.
    # Prefer the refresh cookie (always long-lived + present); the
    # access JWT may already be expired at sign-out time, in which
    # case verify_access yields nothing and the DB row would otherwise
    # survive for the full refresh TTL.
    token = request.cookies.get(deps.SESSION_COOKIE)
    claims = sessions.verify_access(token) if token else None
    refresh = request.cookies.get(deps.REFRESH_COOKIE)
    revoked = sessions.revoke_by_refresh(refresh, "signout") if refresh else False
    if not revoked and claims:
        sessions.revoke_session(claims["sid"], "signout")
    # Access log (best-effort; only when we can attribute it — a signout
    # after the access JWT already expired can't name the user).
    if claims:
        try:
            audit.log(None, claims.get("sub"), claims.get("name"),
                      "web_signout", {"surface": "web"})
        except Exception:
            log.warning("audit: web_signout failed", exc_info=True)
    resp = Response(status_code=204)
    resp.delete_cookie(deps.SESSION_COOKIE, path="/")
    resp.delete_cookie(deps.REFRESH_COOKIE, path="/api/auth")
    return resp

# ---- data endpoints (Phase 2+) --------------------------------------

