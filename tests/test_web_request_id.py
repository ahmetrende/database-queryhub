"""X-Request-ID: a server-made id on each web response, in the 500 body, and
in the log lines of the web process.

The properties that matter:

  * Every response carries the header, the unhandled-500 one too. Starlette
    sends that 500 from ServerErrorMiddleware, outside each middleware that
    add_middleware installs, so that case needs its own proof.
  * The server makes the id and ignores a client X-Request-ID: a caller who
    picks the id could make their log lines look like another request's.
  * The header, the 500 body, the log record and the route's context hold one
    value, also in a sync route that runs in a worker thread.
  * The bot's log format does not change. Only the web process asks for ids.
"""
import asyncio
import io
import json
import logging
import re
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

from queryhub import db, logging_setup
from queryhub.web import app as web_app
from queryhub.web import request_id

HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(db, "init_pool", lambda: None)
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    application = web_app.create_app()

    def boom():
        raise RuntimeError("probe failure")

    def whoami():
        logging.getLogger("queryhub.web.probe").info("probe route ran")
        return {"id": request_id.current()}

    def needs_int(n: int):
        return {"n": n}

    def conflict():
        raise HTTPException(status_code=409,
                            detail={"code": "conflict", "message": "Refused."})

    for path, fn in (("/api/_probe/boom", boom), ("/api/_probe/whoami", whoami),
                     ("/api/_probe/int", needs_int), ("/api/_probe/conflict", conflict)):
        application.add_api_route(path, fn, methods=["GET"])
        # First, not last: with a built frontend the static mount at "/"
        # answers before a route that the test appends after it.
        application.router.routes.insert(0, application.router.routes.pop())
    return application


@pytest.fixture
def client(app):
    # raise_server_exceptions=False: show the 500 response that a browser
    # gets, instead of raising the route's exception in the test.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _rid(response) -> str:
    rid = response.headers.get("x-request-id", "")
    assert HEX32.match(rid), (response.status_code, rid)
    return rid


@pytest.mark.parametrize("method,path,kwargs,status", [
    ("GET", "/healthz", {}, 200),
    ("GET", "/api/me", {}, 401),                     # HTTPException handler
    ("GET", "/api/_probe/conflict", {}, 409),        # HTTPException with a dict
    ("GET", "/api/_probe/int?n=x", {}, 422),         # validation handler
    ("GET", "/api/_probe/no-such-route", {}, 404),   # router 404
    # Refused by the CSRF middleware before any route runs.
    ("POST", "/api/queries", {"headers": {"origin": "https://evil.example.com"},
                              "json": {}}, 403),
    ("GET", "/api/_probe/boom", {}, 500),            # unhandled exception
])
def test_every_response_carries_a_server_made_id(client, method, path, kwargs, status):
    r = client.request(method, path, **kwargs)
    assert r.status_code == status
    _rid(r)


def test_a_client_supplied_id_is_ignored(client):
    r = client.get("/api/_probe/whoami", headers={"X-Request-ID": "forged-0001"})
    rid = _rid(r)
    assert rid != "forged-0001"
    assert r.json()["id"] == rid


def test_each_request_gets_a_new_id(client):
    ids = {_rid(client.get("/healthz")) for _ in range(5)}
    assert len(ids) == 5


def test_the_unhandled_500_has_the_id_in_the_header_the_body_and_the_log(client, caplog):
    caplog.set_level(logging.ERROR, logger="queryhub.web.app")
    r = client.get("/api/_probe/boom")
    rid = _rid(r)
    assert r.status_code == 500
    # The existing fields stay as they were. The id is one more field.
    assert r.json() == {"error": {"code": "server_error",
                                  "message": "Internal error.", "traceId": rid}}
    logged = [rec for rec in caplog.records if rec.name == "queryhub.web.app"
              and "unhandled error" in rec.getMessage()]
    assert len(logged) == 1
    assert rid in logged[0].getMessage()
    assert logged[0].exc_info and "probe failure" in str(logged[0].exc_info[1])


class _Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_a_sync_route_and_its_log_lines_see_the_same_id(client):
    handler = _Records()
    handler.addFilter(request_id.LogFilter())
    probe = logging.getLogger("queryhub.web.probe")
    probe.addHandler(handler)
    probe.setLevel(logging.INFO)
    try:
        r = client.get("/api/_probe/whoami")
    finally:
        probe.removeHandler(handler)
        probe.setLevel(logging.NOTSET)
    rid = _rid(r)
    assert r.json()["id"] == rid
    assert [rec.trace_id for rec in handler.records] == [rid]


# ---- the middleware on its own ----------------------------------------------

def _http_scope():
    return {"type": "http", "method": "GET", "path": "/", "headers": []}


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def test_the_id_replaces_an_inner_value_and_ends_with_the_request():
    seen, sent = [], []

    async def inner(scope, receive, send):
        seen.append(request_id.current())
        await send({"type": "http.response.start", "status": 204,
                    "headers": [(b"x-request-id", b"set-by-a-route")]})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    async def scenario():
        await request_id.RequestIdMiddleware(inner)(_http_scope(), _receive, send)
        # Same task, same context: a missing reset would still show the id.
        return request_id.current()

    assert asyncio.run(scenario()) is None
    headers = [(k, v) for k, v in sent[0]["headers"] if k == b"x-request-id"]
    assert headers == [(b"x-request-id", seen[0].encode())]
    assert HEX32.match(seen[0])


@pytest.mark.parametrize("kind", ["lifespan", "websocket"])
def test_other_scopes_pass_through_without_an_id(kind):
    calls = []

    async def inner(scope, receive, send):
        calls.append((scope["type"], request_id.current(), send))

    async def send(message):
        pass

    asyncio.run(request_id.RequestIdMiddleware(inner)({"type": kind}, _receive, send))
    assert calls == [(kind, None, send)]


# ---- log format -------------------------------------------------------------

@contextmanager
def _inside_a_request(rid):
    token = request_id._current.set(rid)
    try:
        yield
    finally:
        request_id._current.reset(token)


@pytest.fixture
def lines():
    """configure() into a list, then put the root logger back as it was."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    out = io.StringIO()

    def configure(**kwargs):
        logging_setup.configure(level="DEBUG", **kwargs)
        root.handlers[0].setStream(out)
        return out

    yield configure
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def test_web_text_lines_carry_the_id_and_a_dash_outside_a_request(lines):
    out = lines(fmt="text", request_ids=True)
    log = logging.getLogger("queryhub.web.probe")
    with _inside_a_request("0123456789abcdef0123456789abcdef"):
        log.info("inside")
    log.info("outside")
    inside, outside = out.getvalue().splitlines()
    assert " [0123456789abcdef0123456789abcdef]: inside" in inside
    assert " [-]: outside" in outside


def test_web_json_lines_carry_the_id_only_inside_a_request(lines):
    out = lines(fmt="json", request_ids=True)
    log = logging.getLogger("queryhub.web.probe")
    with _inside_a_request("0123456789abcdef0123456789abcdef"):
        log.info("inside")
    log.info("outside")
    inside, outside = (json.loads(x) for x in out.getvalue().splitlines())
    assert inside["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert "trace_id" not in outside


def test_a_query_request_number_in_extra_stays_apart_from_the_trace_id(lines):
    # `request_id` in extra= is the number of a QueryHub request. The trace id has
    # its own field, so neither hides the other.
    out = lines(fmt="json", request_ids=True)
    with _inside_a_request("0123456789abcdef0123456789abcdef"):
        logging.getLogger("t").info("dispatched", extra={"request_id": 4242})
    line = json.loads(out.getvalue())
    assert line["request_id"] == 4242
    assert line["trace_id"] == "0123456789abcdef0123456789abcdef"


def test_the_bot_text_format_does_not_change(lines):
    out = lines(fmt="text")
    with _inside_a_request("0123456789abcdef0123456789abcdef"):
        logging.getLogger("queryhub.executor").info("hello")
    line = out.getvalue().strip()
    assert line.endswith(" INFO queryhub.executor: hello")
    assert "0123456789abcdef" not in line
