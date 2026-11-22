"""Reject an oversized request body with 413, before any route reads it.

Pure ASGI, so nothing has read the body when this runs. The check has two
parts. A Content-Length above the limit is refused at once. A body with no
Content-Length (chunked) is counted as it streams, and the response becomes 413
when the count passes the limit.

The limit is a module constant, not a bot_config key. This middleware runs on
every request, and a config read is a database call on the event loop.
"""
from __future__ import annotations

import json
from typing import Any

# The largest legitimate body is a SQL text of 200,000 characters. That is under
# 1 MB, even in a multi-byte encoding. 16 MB leaves a wide margin.
MAX_BODY_BYTES = 16 * 1024 * 1024

_BODY = json.dumps({"error": {
    "code": "payload_too_large",
    "message": "Refused: the request body is too large."}}).encode()


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int | None = None):
        self.app = app
        self.max_bytes = MAX_BODY_BYTES if max_bytes is None else max_bytes

    async def _refuse(self, send) -> None:
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(_BODY)).encode()),
            (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": _BODY})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    await self._refuse(send)
                    return
                break

        seen = 0
        over = False
        started = False
        answered = False

        async def counting_receive() -> dict[str, Any]:
            nonlocal seen, over
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    over = True
                    raise _TooLarge()
            return msg

        async def guarded_send(msg: dict[str, Any]) -> None:
            nonlocal started, answered
            if answered:
                return
            # A route can turn the read error into its own 400. Replace it.
            if over and not started and msg["type"] == "http.response.start":
                answered = True
                await self._refuse(send)
                return
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except _TooLarge:
            if not started and not answered:
                await self._refuse(send)
