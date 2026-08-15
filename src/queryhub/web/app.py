"""FastAPI app: auth flow + /me now; API_CONTRACT endpoints arrive in
later phases. Run with `python -m queryhub.web`."""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from html import escape as html_escape

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from .. import db
from .. import config as cfg
from . import build_info, deps, routes_auth, routes_avatar
# base_url and the cookie helpers live with the auth routes now; the startup
# log and /api/me still read base_url, so it is imported rather than copied.
from .routes_auth import _must_change_password, base_url

log = logging.getLogger(__name__)











def create_app() -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        """Startup/shutdown. `@app.on_event` is deprecated in FastAPI and slated
        for removal, and it warns on every boot and every test that builds an
        app; this is the supported replacement.

        Both halves are synchronous and blocking on purpose — `db.init_pool()`,
        and a drain that waits on in-flight query executions for up to
        query_timeout_sec. `on_event` with a sync callable ran them in a
        threadpool, so keep doing that rather than blocking the event loop: same
        semantics as before, only the registration mechanism changed.

        _startup/_shutdown are defined further down this function; the closure
        resolves them when the app actually starts, by which point they are
        bound.
        """
        await run_in_threadpool(_startup)
        try:
            yield
        finally:
            # `finally`, so a crash in the app body still drains the executor
            # and stops the scheduler threads.
            await run_in_threadpool(_shutdown)

    app = FastAPI(title="QueryHub Web", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=_lifespan)

    @app.middleware("http")
    async def _security(request: Request, call_next):
        # CSRF defense-in-depth. Session cookies are already
        # SameSite=Lax + HttpOnly, which blocks cross-site cookie-bearing
        # state changes; this also rejects an unsafe-method request whose
        # Origin host doesn't match the request Host, covering clients that
        # ignore SameSite. Compared against the request's own Host (not a
        # config value) so a legitimate same-origin call is never blocked;
        # a missing Origin (non-browser client) falls through to auth.
        #
        # The comparison itself lives in deps.origin_is_same_site, shared with
        # the WebSocket handshake — this middleware only sees http scope, so the
        # handshake needs its own call and the two must not drift.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not deps.origin_is_same_site(request):
                return JSONResponse(status_code=403, content={"error": {
                    "code": "forbidden",
                    "message": "Cross-origin request refused."}})
        resp = await call_next(request)
        # Baseline security headers. The frontend is fully
        # self-contained — bundled JS/CSS, self-hosted fonts, no CDN — so the
        # policy can stay at 'self' with no inline script allowance at all:
        # script-src has no 'unsafe-inline', which is what actually stops a
        # reflected/stored payload from executing. The build stamp that used to
        # need it is now a <meta> tag read by the bundle (see index()).
        #
        # style-src keeps 'unsafe-inline' because React writes inline `style`
        # attributes for measured layout (widths, transforms). Narrowing that to
        # `style-src-attr 'unsafe-inline'` would be tighter, but browsers without
        # style-src-attr fall back to style-src and would strip those attributes,
        # breaking layout — and inline *style* is not an execution vector here
        # (no external stylesheet origins, no `expression()` in any live engine).
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.url.scheme == "https":
            resp.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains")
        return resp

    def _startup() -> None:
        try:
            db.init_pool()
        except Exception:
            # Start degraded rather than crash-looping if the control DB is
            # unreachable at boot: /readyz then reports unready so a
            # probe-driven deployment pulls this instance from rotation, and
            # DB-touching routes 503 until the pool recovers.
            log.exception("control DB unreachable at startup — starting "
                          "degraded; /readyz will report unready")
        # The banner reads bot_config (base_url), i.e. the pool that may have
        # just failed — so it cannot be allowed to raise. It used to: an
        # unreachable control DB was caught above, then this line called
        # base_url() -> get_setting() -> a dead pool, the exception escaped
        # _startup, and the process crash-looped. Exactly the outcome the
        # comment above says is avoided. A log line must never decide whether
        # the service boots.
        try:
            _b = build_info.build()
            log.info("QueryHub Web API started (base_url=%s, build=%s sha=%s)",
                     base_url(), _b.get("version", "?"), _b.get("sha", "?"))
        except Exception:
            log.warning("QueryHub Web API started (build/base_url unavailable)",
                        exc_info=True)

        # Transport-independent worker. The scheduler loop, orphan
        # reconciler, and crash-recovery of 'approved' requests normally live
        # in the Slack process (main.py). In the vanilla profile there is no
        # Slack process, so the web process — which is the one that executes
        # queries there — must own them, or scheduled requests never dispatch
        # and orphans are never recovered. When Slack IS enabled the Slack
        # process owns them; running them here too would double up (dispatch
        # is SKIP-LOCKED-safe, but boot recovery is not), so gate on vanilla.
        if not cfg.ENV.slack_enabled:
            import threading
            from .. import executor
            from .routes_queries import _bot_client
            client = _bot_client()  # None in vanilla; delivery/DMs no-op
            try:
                executor.reconcile_orphaned_executing()
                resubmitted = executor.resubmit_approved_on_boot(client)
                if resubmitted:
                    log.info("web: re-submitted %d orphaned 'approved' "
                             "request(s)", resubmitted)
            except Exception:
                log.exception("web: boot recovery failed")
            stop = threading.Event()
            thread = threading.Thread(
                target=executor.scheduler_loop, args=(client, stop),
                kwargs={"interval_sec": 60}, name="web-scheduler", daemon=True)
            thread.start()
            app.state.scheduler_stop = stop
            log.info("web: scheduler thread started (vanilla profile)")

            # Same reasoning for the authorization-change outbox. Its poller
            # also lived only in the Slack process, so with no Slack nothing
            # ever drained `auth_event_outbox` — every grant/revoke appended a
            # row that was never processed and never removed, growing without
            # bound. The DM side no-ops without a client, so the poller's real
            # job here is to drain and mark the rows.
            from .. import auth_events
            auth_stop = threading.Event()
            threading.Thread(
                target=auth_events.poll_loop, args=(client, auth_stop),
                name="web-auth-events", daemon=True).start()
            app.state.auth_events_stop = auth_stop
            log.info("web: auth-event poller started (vanilla profile)")

        try:
            from ..slack_app import notifications
            from .routes_queries import _bot_client
            notifications.dm_all_admins(
                _bot_client(), ":arrows_counterclockwise: *QueryHub web* "
                "restarted and is back online.")
        except Exception:
            log.exception("web startup admin DM failed")

    def _shutdown() -> None:
        # Graceful drain: refuse new submissions, tell admins, then wait for
        # this process's in-flight query executions to finish (bounded by
        # systemd TimeoutStopSec). Uvicorn has already stopped accepting new
        # HTTP connections by the time this runs.
        from .. import executor, lifecycle
        lifecycle.begin_drain()
        # Stop the vanilla-profile scheduler loop (if this process owns one)
        # before draining the pool, so it doesn't dispatch new work mid-drain.
        for attr in ("scheduler_stop", "auth_events_stop"):
            ev = getattr(app.state, attr, None)
            if ev is not None:
                ev.set()
        # No "stopping" DM here — a per-admin fan-out would slow the restart.
        # The startup DM already signals the restart. Just finish in-flight.
        try:
            executor.shutdown()
        except Exception:
            log.exception("web executor drain failed")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={
            "error": {"code": "server_error", "message": "Internal error."}})

    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {
            "code": "server_error", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default 422 is {"detail":[...]} — reshape to the
        # contract envelope {"error":{code,message}} the frontend parses,
        # with a short human message pointing at the first bad field.
        errs = exc.errors()
        first = errs[0] if errs else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        msg = first.get("msg", "Invalid request.")
        return JSONResponse(status_code=422, content={"error": {
            "code": "validation",
            "message": (f"{loc}: {msg}" if loc else msg),
        }})

    # ---- routers ------------------------------------------------------
    # Auth (login, OAuth round-trip, refresh, sign-out) is routes_auth;
    # it is a router like the rest rather than a block inside this factory.
    from . import routes_admin, routes_data, routes_queries, routes_requests
    app.include_router(routes_auth.router)

    # Avatar bytes come from our own origin so the CSP can stay at
    # `img-src 'self'` and no third party sees the viewer. See
    # routes_avatar for why this is a proxy and not a CSP exception.
    app.include_router(routes_avatar.router)
    app.include_router(routes_data.router)
    app.include_router(routes_queries.router)
    app.include_router(routes_requests.router)
    app.include_router(routes_admin.router)

    # ---- health / readiness ---------------------------------------------
    # Unauthenticated, cheap probes for process managers and load balancers.
    # /healthz = liveness (the process is up); /readyz = readiness (the
    # control-plane DB is reachable) — 503 when it is not, so a restarting or
    # DB-cut instance is pulled from rotation instead of serving 500s.
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            db.fetch_one("SELECT 1 AS ok")
        except Exception:
            log.warning("readiness probe: control DB unreachable", exc_info=True)
            return JSONResponse(status_code=503, content={"status": "unready",
                                "detail": "control database unreachable"})
        return {"status": "ready"}

    # ---- operational metrics -------------------------------------------
    #
    # Off by default. A self-hosted tool must not start exposing its queue
    # depth, fleet size and user counts because someone upgraded — turning it
    # on is a decision, so `web_metrics_enabled` gates it and the route 404s
    # when disabled rather than 403ing (a 403 confirms the endpoint exists).
    #
    # Authentication, in order of preference:
    #   * `web_metrics_token` set -> require `Authorization: Bearer <token>`.
    #     This is the practical option: Prometheus can send a bearer token, and
    #     it cannot hold a session cookie.
    #   * no token -> require an admin session, so enabling the key without
    #     setting a token does not publish the endpoint.
    @app.get("/metrics")
    def metrics(request: Request):
        from . import ops_metrics

        enabled = (cfg.get_setting("web_metrics_enabled", "off")
                   or "").strip().lower()
        if enabled not in {"on", "1", "true", "yes"}:
            raise deps._error(404, "not_found", "Not found.")

        token = (cfg.get_setting("web_metrics_token", "") or "").strip()
        if token:
            auth = request.headers.get("authorization", "")
            presented = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            # Constant-time compare: this is a bearer credential, and a length
            # or prefix oracle is exactly what a scraper endpoint should not be.
            import hmac
            if not presented or not hmac.compare_digest(presented, token):
                raise deps._error(401, "unauthenticated", "Invalid metrics token.")
        else:
            claims = deps.current_user(request)
            from . import admin as admin_mod
            admin_mod.require_admin(claims, "review")

        return PlainTextResponse(
            ops_metrics.render(),
            # The version suffix is part of the format contract; Prometheus
            # accepts text/plain either way, but being explicit documents which
            # exposition version this is.
            media_type="text/plain; version=0.0.4; charset=utf-8")

    # ---- identity ------------------------------------------------------

    @app.get("/api/me")
    def me(claims: dict = Depends(deps.current_user)):
        deps.require_whitelisted(claims)
        name = claims.get("name") or claims["sub"]
        initials = "".join(w[0].upper() for w in str(name).split()[:2]) or "?"
        from . import admin as admin_mod
        block = admin_mod.admin_block(claims["sub"])
        out = {"user": {
            "slackId": claims["sub"],
            "name": name,
            "email": claims.get("email"),
            "initials": initials,
            # Org label shown in the nav. Config-driven so the code carries no
            # deployment's name; set bot_config.web_org_label per install.
            "team": cfg.get_setting("web_org_label", "QueryHub") or "QueryHub",
            # UI role hint: "super" unlocks the super-admin affordances in the
            # web app (full-access badge, direct-run copy). It is only a hint —
            # the server enforces every action from the session, never this.
            "role": (block or {}).get("role", "developer"),
            # Slack avatar (image_192) from the session; UI falls back to
            # initials when null. Cosmetic — never used for authorization.
            # Our own endpoint, not the Slack CDN URL. The CSP is
            # `img-src 'self'`, so a slack-edge.com URL here was blocked by
            # the browser and silently fell back to initials from 2026-07-24
            # (the hardening commit) until this was noticed. The proxy 404s
            # when there is no avatar, and the UI still falls back then.
            "avatar": "/api/avatar" if claims.get("avatar") else None,
            # Local accounts: whether the frontend must force a password
            # change before anything else. Slack/other providers: always false.
            "provider": claims.get("provider"),
            "mustChangePassword": _must_change_password(claims),
        }}
        # Fleet-wide display timezone (DB stores UTC; the client formats every
        # shown timestamp in this zone). Storage is always UTC; this is display
        # only, and the default is UTC because the author's timezone is not a
        # sensible default for anyone else. Set web_display_timezone per install.
        out["displayTz"] = cfg.get_setting("web_display_timezone", "UTC") or "UTC"
        # Is there a Slack side at all? The UI has three places that tell the
        # user "Approvals still run in Slack" UNCONDITIONALLY, and the default
        # install profile has no Slack — so the product contradicted itself in
        # its own home screen, to exactly the audience least able to tell which
        # half was true. The flag is served here rather than guessed in the
        # frontend because this is the same value the backend gates every Slack
        # code path on, so the copy cannot drift from the behaviour.
        out["slackEnabled"] = bool(cfg.ENV.slack_enabled)
        if block is not None:
            out["admin"] = block   # drives the Developer↔Admin toggle + super-only nav
        return out

    @app.get("/api/changelog")
    def changelog_feed(claims: dict = Depends(deps.current_user)):
        # Developer-facing "What's new" feed, derived live from git history
        # (see changelog.py) — always current, no scheduled publish job.
        deps.require_whitelisted(claims)
        from . import changelog as _changelog
        # Approver-only items (queue ordering, admin screens) are served only to
        # admins: a requester cannot reach an approval queue, so reading about
        # one is noise between the entries that do concern them.
        from .. import admins as _admins
        try:
            is_approver = bool(_admins.is_admin(claims["sub"]))
        except Exception:
            is_approver = False
        return {"releases": _changelog.releases(for_approver=is_approver)}

    # ---- static frontend ----------------------------------------------
    # Registered LAST so /api/* always wins. The Vite build
    # (QueryHubWeb/app/dist) is the ONLY servable frontend: it is
    # self-contained, minified and needs no network egress.
    #
    # There is deliberately no fallback to the raw babel-in-browser prototype
    # any more. That prototype loads React and Babel from unpkg.com and fonts
    # from Google, all of which this app's own CSP blocks — so "falling back"
    # to it served a blank page with console errors and a log line the browser
    # user never saw. A missing build is now an explicit, self-explaining
    # error page instead of a silent white screen.

    from pathlib import Path

    from starlette.staticfiles import StaticFiles

    # QH_WEB_STATIC_DIR (env) wins: the frontend lives outside the Python
    # package (repo-root QueryHubWeb/), so an install that isn't run from the
    # source tree — e.g. a wheel in site-packages, where the repo-relative
    # path below resolves to nothing — points here at wherever the
    # built assets were placed. Falls back to the source-checkout layout.
    override = (os.environ.get("QH_WEB_STATIC_DIR") or "").strip()
    qhweb = Path(override) if override else (
        Path(__file__).resolve().parents[3] / "QueryHubWeb")
    dist = qhweb / "app" / "dist"
    if (qhweb / "index.html").is_file() and override:
        # An override pointed straight at a built dist directory.
        static_dir, index_file = qhweb, qhweb / "index.html"
        log.info("serving frontend from QH_WEB_STATIC_DIR=%s", qhweb)
    elif (dist / "index.html").is_file():
        static_dir, index_file = dist, dist / "index.html"
        log.info("serving Vite build from %s", dist)
    else:
        log.error(
            "frontend build missing under %s — build it with "
            "`cd QueryHubWeb/app && npm install && npm run build`, or point "
            "QH_WEB_STATIC_DIR at a built directory. The API keeps serving.",
            dist)
        static_dir = index_file = None

    if static_dir is not None:
        @app.get("/")
        def index():
            # index.html must never be cached: it points at content-hashed
            # JS/CSS bundles, so a stale cached copy pins the browser to an
            # old build (a classic "why isn't my deploy showing?" trap). The
            # hashed assets themselves are immutable and cache fine.
            #
            # Stamp the deployed build (the git commit the service runs out of)
            # into the page so the UI shows it — the backend is the source of
            # truth, no hand-set client constant. The client reader
            # (qh-version.js, after qh-data.jsx) prefers it over the design's
            # hardcoded QH_VERSION.
            #
            # A meta tag, not an injected <script>: an inline script here would
            # force `script-src 'unsafe-inline'` into the CSP for the sake of two
            # assignments, which is exactly the hole an XSS needs.
            html = index_file.read_text(encoding="utf-8")
            b = build_info.build()
            if b.get("version") and "</head>" in html:
                # quote=True escapes & < > " ' — the JSON sits in a
                # double-quoted attribute, so a quote in a branch or repo name
                # cannot break out of it.
                stamp = html_escape(json.dumps(b), quote=True)
                tag = '<meta name="qh-build" content="' + stamp + '">'
                html = html.replace("</head>", tag + "</head>", 1)
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})

        app.mount("/", StaticFiles(directory=str(static_dir)), name="static")
    else:
        # No build present. Say so, in the browser, with the fix — instead of
        # the blank page the old CDN fallback produced. Plain text/HTML with
        # no external assets so it renders under the CSP.
        @app.get("/")
        def index_missing_build():
            return HTMLResponse(
                "<h1>QueryHub — frontend not built</h1>"
                "<p>The API is running, but the web UI bundle is missing.</p>"
                "<pre>cd QueryHubWeb/app &amp;&amp; npm install &amp;&amp; npm run build</pre>"
                "<p>Then restart this service. Alternatively set "
                "<code>QH_WEB_STATIC_DIR</code> to a directory that already "
                "contains a built <code>index.html</code>.</p>",
                status_code=503, headers={"Cache-Control": "no-store"})

    return app
