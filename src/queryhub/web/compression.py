"""gzip for the text this app sends, and only the text.

The first screen loads `/api/connections` and the JS bundle. For a reader with
many connections that was 2 MB of JSON and 750 KB of script, sent as they were.
Over an SSH tunnel or a VPN, the sidebar stayed empty for seconds while they
crossed. gzip makes the JSON about 11 times smaller.

This is its own small middleware, not a subclass of Starlette's. Starlette's
GZipMiddleware changes inside between releases (1.3 and 1.9 order their
messages differently), and a subclass that reaches into it broke on the newer
one. This one uses only the ASGI messages, so it behaves the same on both.

It packs only types made of text. A spreadsheet is a zip file already, and a
font or an image is compressed too: packing them again costs CPU and saves
nothing. Three kinds of response are never packed, even when the type is text:

- A partial answer (a `Range` or `If-Range` request, status 206). `Content-Range`
  counts bytes of the plain file, so a gzip body would corrupt a resumed
  download.
- A download (`Content-Disposition: attachment`). It keeps its `Content-Length`,
  so the browser can show progress, and the server does not pack up to 100 MB on
  the event loop.
- A response that already has a `Content-Encoding`.

BREACH needs an attacker to read the size of a compressed response that holds
both a secret and text the attacker chose. No response body here carries a
secret: the session is an HttpOnly cookie with SameSite=Lax, so a request from
another site carries no cookie, and the build stamp in `index.html` holds only
build metadata. Some answers do echo input (an error or a search term), but
there is no secret next to it to measure.
"""
from __future__ import annotations

import zlib
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Content types worth compressing. Anything else passes through unchanged.
COMPRESSIBLE_PREFIXES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "application/x-ndjson",
    "image/svg+xml",
)
#: An event stream is text, and must not be buffered: a client waits on each event.
NEVER_PACKED = ("text/event-stream",)


def is_compressible(content_type: str) -> bool:
    ctype = (content_type or "").strip().lower()
    return ctype.startswith(COMPRESSIBLE_PREFIXES) and not ctype.startswith(NEVER_PACKED)


def _must_stay_plain(message: Message) -> bool:
    """True for a response that must reach the client byte for byte."""
    headers = Headers(raw=message["headers"])
    return (not is_compressible(headers.get("content-type", ""))
            or message.get("status") == 206
            or "content-range" in headers
            or "content-encoding" in headers
            or headers.get("content-disposition", "").lstrip().lower().startswith("attachment"))


class TextOnlyGZipMiddleware:
    """Pack text responses with gzip when the client accepts it."""

    def __init__(self, app: ASGIApp, minimum_size: int = 1024, compresslevel: int = 5) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        # A request for a byte range, or one that depends on a validator, wants
        # the file as it is. `If-Range` always comes with `Range`; both are checked.
        if ("gzip" not in headers.get("accept-encoding", "")
                or "range" in headers or "if-range" in headers):
            await self.app(scope, receive, send)
            return
        await _Responder(send, self.minimum_size, self.compresslevel)(self.app, scope, receive)


class _Responder:
    """Sits between the app and the client for one response."""

    def __init__(self, send: Send, minimum_size: int, compresslevel: int) -> None:
        self.send = send
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel
        self.initial: Message | None = None     # start message, held until we know
        self.plain = False                      # this response is passed through
        self.compressor: Any = None             # a zlib compressor, set once we start packing

    async def __call__(self, app: ASGIApp, scope: Scope, receive: Receive) -> None:
        await app(scope, receive, self._on_message)

    async def _flush_initial(self) -> None:
        if self.initial is not None:
            initial, self.initial = self.initial, None
            await self.send(initial)

    async def _on_message(self, message: Message) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            if _must_stay_plain(message):
                self.plain = True
                await self.send(message)
            else:
                self.initial = message          # decided on the first body message
            return
        if self.plain or kind != "http.response.body":
            # `http.response.pathsend` and other extensions are never packed.
            await self._flush_initial()
            await self.send(message)
            return

        body = message.get("body", b"")
        more = message.get("more_body", False)
        if self.compressor is None and self.initial is not None:
            headers = MutableHeaders(raw=self.initial["headers"])
            headers.add_vary_header("Accept-Encoding")
            if len(body) < self.minimum_size and not more:
                # Too small to be worth it. Sent as it is.
                self.plain = True
                await self._flush_initial()
                await self.send(message)
                return
            self.compressor = zlib.compressobj(self.compresslevel, zlib.DEFLATED, 31)
            headers["Content-Encoding"] = "gzip"
            if not more:
                packed = self.compressor.compress(body) + self.compressor.flush()
                headers["Content-Length"] = str(len(packed))
                await self._flush_initial()
                await self.send({**message, "body": packed})
                return
            del headers["Content-Length"]       # the length is not known yet
            await self._flush_initial()
        if self.compressor is None:             # no start message was ever sent
            await self.send(message)
            return
        packed = self.compressor.compress(body)
        if not more:
            packed += self.compressor.flush()
        await self.send({**message, "body": packed})
