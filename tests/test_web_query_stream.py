"""The live-status WebSocket: that it connects at all, and who it refuses.

It did not connect. `routes_queries.router` carries
`dependencies=[Depends(deps.block_pw_gate)]`, which depends on `current_user`,
which was annotated `request: Request` — and FastAPI cannot supply a Request to
a WebSocket route. Every handshake on /api/queries/{id}/stream raised

    TypeError: current_user() missing 1 required positional argument: 'request'

and was rejected with a 500. Verified against the running service, not just in
a test: `curl` with the upgrade headers returned 500 and the journal carried
that traceback.

Nothing reported it because the frontend falls back to HTTP polling when the
socket will not open. The feature degraded to the thing it was built to replace,
silently, and stayed that way. No test covered the handshake — which is the
actual finding here, so this file exists mostly to make sure the socket is
exercised at all.

The second property is Origin. `@app.middleware("http")` only sees http scope,
so the CSRF check every other route gets does not apply to a handshake.
SameSite=Lax stops a cross-site script-initiated upgrade from carrying the
cookie in a current browser, so the check is defence-in-depth — but that cookie
attribute is otherwise the only thing between a page on another origin and a
live status stream for someone else's query.
"""
import logging

import pytest
from starlette.testclient import TestClient

from queryhub import admins, db, requesters
from queryhub.web import app as web_app
from queryhub.web import routes_queries, sessions

OWNER = "U0OWNER"


@pytest.fixture
def client(monkeypatch):
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)

    monkeypatch.setattr(sessions, "verify_access",
                        lambda t: {"sub": OWNER, "sid": "sid-1",
                                   "provider": "slack"} if t == "good" else None)
    monkeypatch.setattr(sessions, "session_alive", lambda sid, principal=None: True)
    monkeypatch.setattr(routes_queries.sessions, "verify_access",
                        lambda t: {"sub": OWNER, "sid": "sid-1",
                                   "provider": "slack"} if t == "good" else None)
    monkeypatch.setattr(routes_queries.sessions, "session_alive", lambda sid, principal=None: True)
    monkeypatch.setattr(admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(routes_queries.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(routes_queries.requesters, "is_allowed", lambda uid: True)

    # Ownership check + the first poll tick. A terminal status makes the handler
    # send one message and stop, so the test does not wait on the poll loop.
    def fake_fetch_one(sql, params=None):
        if "requester_slack_id" in sql and "SELECT 1" in sql:
            return {"x": 1}
        return {"status": "completed", "scheduled_for": None,
                "executed_at": None, "decided_by_slack_id": None,
                "decided_by_name": None}

    monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(routes_queries.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(routes_queries.db, "fetch_all", lambda *a, **k: [])

    with TestClient(web_app.create_app()) as c:
        c.cookies.set("qh_session", "good")
        yield c


def test_the_handshake_succeeds(client):
    """The regression. A 500 here means the router dependency cannot resolve in
    WebSocket scope again."""
    with client.websocket_connect(
            "/api/queries/42/stream",
            headers={"origin": "http://testserver"}) as ws:
        msg = ws.receive_json()
    assert msg["type"] == "status"
    assert msg["id"] == "42"


def test_a_cross_origin_handshake_is_refused(client):
    """The middleware cannot do this one — it never sees websocket scope."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as err:
        with client.websocket_connect(
                "/api/queries/42/stream",
                headers={"origin": "https://evil.example.com"}) as ws:
            ws.receive_json()
    assert err.value.code == 4403


def test_a_handshake_with_no_origin_is_allowed_then_authenticated(client):
    """A non-browser client sends no Origin and carries no ambient cookie, so
    refusing on absence would only break legitimate tooling. Same choice the
    HTTP middleware makes — the two share one helper so they cannot diverge."""
    with client.websocket_connect("/api/queries/42/stream") as ws:
        assert ws.receive_json()["type"] == "status"


def test_an_unauthenticated_handshake_is_refused_not_crashed(client):
    """Refused with 401, not 500.

    The distinction is the whole point of the fix. Before, the router dependency
    raised a TypeError before any of the handler ran, so EVERY handshake —
    authenticated or not — died the same way with a 500. Now the dependency
    resolves, finds no session, and denies the upgrade with a proper 401.

    That arrives as a WebSocketDenialResponse rather than a close frame, because
    the rejection happens during the HTTP handshake and never becomes a
    WebSocket at all. `_ws_auth`'s own 4401 close covers the same case one layer
    in; both are correct, and which one fires depends on whether the dependency
    or the handler notices first.
    """
    client.cookies.clear()
    with pytest.raises(Exception) as err:
        with client.websocket_connect(
                "/api/queries/42/stream",
                headers={"origin": "http://testserver"}) as ws:
            ws.receive_json()
    status = getattr(err.value, "status_code", None)
    code = getattr(err.value, "code", None)
    assert status == 401 or code == 4401, \
        f"expected a 401 denial or a 4401 close, got status={status} code={code}"
    assert status != 500, "a dependency blew up instead of refusing"


def test_a_stream_for_someone_elses_request_is_refused(client, monkeypatch):
    """The ownership check. Without it, knowing a request id would be enough to
    watch another user's query."""
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(routes_queries.db, "fetch_one",
                        lambda sql, params=None: None)
    with pytest.raises(WebSocketDisconnect) as err:
        with client.websocket_connect(
                "/api/queries/999/stream",
                headers={"origin": "http://testserver"}) as ws:
            ws.receive_json()
    assert err.value.code == 4404


def test_a_session_revoked_mid_stream_closes_the_socket(client, monkeypatch):
    """The session is re-checked during the stream, not only at the handshake."""
    from starlette.websockets import WebSocketDisconnect

    state = {"revoked": False}

    def fake_fetch_one(sql, params=None):
        if "requester_slack_id" in sql and "SELECT 1" in sql:
            return {"x": 1}
        state["revoked"] = True  # revoke after the first poll
        return {"status": "pending", "scheduled_for": None,
                "executed_at": None, "decided_by_slack_id": None,
                "decided_by_name": None}

    monkeypatch.setattr(routes_queries.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(sessions, "session_alive",
                        lambda sid, principal=None: not state["revoked"])
    monkeypatch.setattr(routes_queries, "_WS_POLL_SEC", 0.0)
    monkeypatch.setattr(routes_queries, "_WS_RECHECK_TICKS", 1)
    with pytest.raises(WebSocketDisconnect) as err:
        with client.websocket_connect(
                "/api/queries/42/stream",
                headers={"origin": "http://testserver"}) as ws:
            for _ in range(20):
                ws.receive_json()
    assert err.value.code == 1008


def test_a_user_the_own_ui_gate_refuses_is_closed_at_the_handshake(
        client, monkeypatch):
    from queryhub.web import deps

    monkeypatch.setattr(deps, "own_ui_allowed", lambda uid: False)
    with pytest.raises(Exception) as err:
        with client.websocket_connect(
                "/api/queries/42/stream",
                headers={"origin": "http://testserver"}) as ws:
            ws.receive_json()
    # Either the router dependency denies the upgrade (403) or the handler
    # closes it (4403). Both refuse the user.
    status = getattr(err.value, "status_code", None)
    code = getattr(err.value, "code", None)
    assert status == 403 or code == 4403, (status, code)


def test_origin_helper_semantics():
    """Unit-level, because both the middleware and the handshake depend on it."""
    from queryhub.web import deps

    def conn(origin=None, host="queryhub.example.com"):
        headers = {"host": host}
        if origin is not None:
            headers["origin"] = origin
        return type("C", (), {"headers": headers})()

    assert deps.origin_is_same_site(conn(origin="https://queryhub.example.com"))
    assert deps.origin_is_same_site(conn(origin=None))
    assert not deps.origin_is_same_site(conn(origin="https://evil.example.com"))
    # A host that merely CONTAINS the origin must not pass.
    assert not deps.origin_is_same_site(
        conn(origin="https://queryhub.example.com.evil.test"))
    # Port is part of the origin, so a different port is a different origin.
    assert not deps.origin_is_same_site(
        conn(origin="https://queryhub.example.com:9999"))
    # Case-insensitive, since hostnames are.
    assert deps.origin_is_same_site(conn(origin="https://QueryHub.Example.COM"))
