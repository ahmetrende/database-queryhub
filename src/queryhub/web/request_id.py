"""A server-made id for each HTTP request, sent back as X-Request-ID.

A user who reports an error can give the id, and the operator finds the log
lines of that request with it. The id is an HTTP trace id. It is not the number
of a QueryHub query request.

The server makes a new id for each request. It ignores an X-Request-ID header
from the client: a caller who chooses the id could make their log lines look
like the log lines of another request.

The middleware is pure ASGI, so the id is a contextvar of the request task.
Starlette copies that context into the threads that run sync routes, so their
log lines carry the id too. TracedFastAPI puts the middleware outside each
other layer, also outside Starlette's ServerErrorMiddleware. That layer sends
the 500 response for an unhandled exception with the original `send`, so a
middleware inside it never sees that response. In that position, the one
response that users report would have no id.
"""
from __future__ import annotations

import contextvars
import logging
import uuid

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "X-Request-ID"

# The text log format of the web process (logging_setup.configure with
# request_ids=True). A line from outside a request shows "-" for the id.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(trace_id)s]: %(message)s"

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "queryhub_http_request_id", default=None)


def current() -> str | None:
    """The id of the HTTP request that this code serves. None outside a request."""
    return _current.get()


class LogFilter(logging.Filter):
    """Adds `trace_id` to each log record made while a request runs.

    The field is `trace_id`, not `request_id`: a `request_id` in `extra=` is the
    number of a QueryHub request, and the two stay apart. A `trace_id` that the
    caller gives with `extra=` stays as it is."""

    def filter(self, record: logging.LogRecord) -> bool:
        rid = _current.get()
        if rid is not None and not hasattr(record, "trace_id"):
            record.trace_id = rid
        return True


class RequestIdMiddleware:
    """Gives each HTTP request a new id and adds it to the response headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = uuid.uuid4().hex
        token = _current.set(rid)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                # Replaces a value from an inner layer: the id from this
                # middleware is the only one.
                MutableHeaders(scope=message)[HEADER] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _current.reset(token)


class TracedFastAPI(FastAPI):
    """FastAPI with RequestIdMiddleware outside each other layer."""

    def build_middleware_stack(self) -> ASGIApp:
        return RequestIdMiddleware(super().build_middleware_stack())
