"""current_user's assertion branch, and the guarantee it depends on.

Two things are pinned here. First, that a proxied request is judged ONLY by
its assertion — never falling back to a cookie that happens to be present.
Second, that reading the request body in middleware still leaves the body
readable by the route: Starlette's BaseHTTPMiddleware replays a body that
body() consumed, and the seam's bh binding is built on that. If a future
Starlette drops the replay, the last test here fails instead of the feature
silently truncating every POST.
"""
import pytest
from fastapi import Request as FastAPIRequest
from fastapi.testclient import TestClient
from starlette.requests import Request

from queryhub.web import app as web_app
from queryhub.web import deps, idp_assertion


def _conn(headers: dict, cookies: dict | None = None, *,
          method: str = "GET", path: str = "/api/queue",
          query: bytes = b"", body: bytes = b""):
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    if cookies:
        raw.append((b"cookie",
                    "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({"type": "http", "method": method, "path": path,
                    "query_string": query, "headers": raw, "_body": body})


def test_assertion_header_yields_the_resolved_principal(monkeypatch):
    monkeypatch.setattr(deps.idp_assertion, "verify",
                        lambda *a, **k: idp_assertion.Principal("U123", "Deniz Arslan"))
    claims = deps.current_user(_conn({deps.ASSERTION_HEADER: "tok"}))
    assert claims["sub"] == "U123"
    assert claims["provider"] == "idp"
    assert claims["sid"] is None
    # Carried so `claims.get("name") or uid` renders a person rather than a
    # raw principal id — 41 call sites read it.
    assert claims["name"] == "Deniz Arslan"


def test_a_refused_assertion_is_401_and_does_not_fall_back(monkeypatch):
    """Falling through to the cookie would let a stale browser session stand
    in for a refused proxy call."""
    def boom(*a, **k):
        raise deps.idp_assertion.AssertionError_("replayed")
    monkeypatch.setattr(deps.idp_assertion, "verify", boom)
    monkeypatch.setattr(deps.sessions, "verify_access",
                        lambda t: pytest.fail("cookie path must not run"))
    with pytest.raises(Exception) as e:
        deps.current_user(_conn({deps.ASSERTION_HEADER: "tok"},
                                {deps.SESSION_COOKIE: "cookie-token"}))
    assert e.value.status_code == 401


def test_the_verified_target_includes_the_query_string(monkeypatch):
    """The panel signs the full request target; verifying a bare path would
    accept an assertion minted for a different query."""
    seen = {}

    def capture(token, method, path, body):
        seen.update(method=method, path=path, body=body)
        return idp_assertion.Principal("U123", "Deniz Arslan")

    monkeypatch.setattr(deps.idp_assertion, "verify", capture)
    deps.current_user(_conn({deps.ASSERTION_HEADER: "tok"},
                            method="POST", path="/api/queries",
                            query=b"dry=1", body=b'{"sql":"SELECT 1"}'))
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/queries?dry=1"
    assert seen["body"] == b'{"sql":"SELECT 1"}'


def test_cookie_path_still_works_and_still_binds_the_principal(monkeypatch):
    monkeypatch.setattr(deps.sessions, "verify_access",
                        lambda t: {"sub": "U123", "sid": "s1", "provider": "slack"})
    seen = {}
    monkeypatch.setattr(
        deps.sessions, "session_alive",
        lambda sid, sub=None: seen.update(sid=sid, sub=sub) or True)
    claims = deps.current_user(_conn({}, {deps.SESSION_COOKIE: "cookie-token"}))
    assert claims["sub"] == "U123"
    assert seen == {"sid": "s1", "sub": "U123"}, \
        "the principal must still reach session_alive"


def test_no_credential_is_401():
    with pytest.raises(Exception) as e:
        deps.current_user(_conn({}))
    assert e.value.status_code == 401


def test_middleware_caches_the_body_without_stealing_it(monkeypatch):
    """The guarantee the bh binding rests on.

    The middleware reads the body so current_user can hash it. Starlette
    replays a consumed body to the downstream app; if that ever stops being
    true, every proxied POST would reach its route empty. Assert both halves:
    the dependency saw the bytes, and so did the route.
    """
    monkeypatch.setattr(deps.idp_assertion, "verify", lambda *a, **k: "U123")
    app = web_app.create_app()
    seen = {}

    @app.post("/api/_probe_body")
    async def _probe(request: FastAPIRequest):
        seen["route_body"] = await request.body()
        seen["dep_body"] = request.scope.get("_body")
        return {"ok": True}

    client = TestClient(app)
    r = client.post("/api/_probe_body", content=b'{"sql":"SELECT 1"}',
                    headers={deps.ASSERTION_HEADER: "tok",
                             "Content-Type": "application/json"})

    assert r.status_code == 200
    assert seen["dep_body"] == b'{"sql":"SELECT 1"}', \
        "current_user would hash an empty body"
    assert seen["route_body"] == b'{"sql":"SELECT 1"}', \
        "the route received an empty body — Starlette stopped replaying it"
