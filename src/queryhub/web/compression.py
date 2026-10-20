"""gzip for the text this app sends.

The first screen loads `/api/connections` and the JS bundle. For a reader with
many connections that was 2 MB of JSON and 750 KB of script, sent as they were.
Over an SSH tunnel or a VPN, the sidebar stayed empty for seconds while they
crossed. gzip makes the JSON about 11 times smaller.

Starlette's own middleware compresses everything except an event stream. That
is wrong for this app: a spreadsheet download is a zip file already, and a font
or an image is compressed too. Packing them again costs CPU and saves nothing.
This wrapper packs only types made of text, and leaves the rest alone.

Three kinds of response are never packed, even when the type is text:

- A partial answer (`Range` request, status 206). `Content-Range` counts bytes
  of the plain file, so a gzip body would corrupt a resumed download.
- A download (`Content-Disposition: attachment`). It keeps its `Content-Length`,
  so the browser can show progress, and the server does not pack up to 100 MB on
  the event loop.
- Anything whose type is not text (see below).

BREACH needs an attacker to read the size of a compressed response that holds
both a secret and text the attacker chose. No response body here carries a
secret: the session is an HttpOnly cookie with SameSite=Lax, so a request from
another site carries no cookie, and the build stamp in `index.html` holds only
build metadata. Some answers do echo input (an error or a search term), but
there is no secret next to it to measure.
"""
from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder, IdentityResponder
from starlette.types import Message, Receive, Scope, Send

#: Content types worth compressing. Anything else passes through unchanged.
COMPRESSIBLE_PREFIXES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "application/x-ndjson",
    "image/svg+xml",
)


def is_compressible(content_type: str) -> bool:
    return (content_type or "").strip().lower().startswith(COMPRESSIBLE_PREFIXES)


def _must_stay_plain(message: Message) -> bool:
    """True for a response that must reach the client byte for byte."""
    headers = Headers(raw=message["headers"])
    return (not is_compressible(headers.get("content-type", ""))
            or message.get("status") == 206
            or "content-range" in headers
            or headers.get("content-disposition", "").lstrip().lower().startswith("attachment"))


class _TextOnlyResponder(GZipResponder):
    async def send_with_compression(self, message: Message) -> None:
        # The parent reads the headers on the start message and sets its own
        # exclusion flag. Run it first, then add ours.
        await super().send_with_compression(message)
        if message["type"] == "http.response.start" and _must_stay_plain(message):
            self.content_type_is_excluded = True


class TextOnlyGZipMiddleware(GZipMiddleware):
    """`GZipMiddleware` that compresses text types only."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        # A request for a byte range, or one that depends on a validator, wants
        # the file as it is. `If-Range` always comes with `Range`; both are checked.
        wants_plain = "range" in headers or "if-range" in headers
        accepts = "gzip" in headers.get("Accept-Encoding", "") and not wants_plain
        responder = (_TextOnlyResponder(self.app, self.minimum_size,
                                        compresslevel=self.compresslevel)
                     if accepts else IdentityResponder(self.app, self.minimum_size))
        await responder(scope, receive, send)
