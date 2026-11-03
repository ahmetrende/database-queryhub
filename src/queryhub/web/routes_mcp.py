"""The MCP door: MCP over Streamable HTTP at `/mcp`, for the portal.

A person connects Claude Code to the portal (the IDP panel) with a personal
access token. The portal answers `initialize` and `tools/list` itself. It
sends each tool call here with its identity assertion (`x-idp-assertion`),
bound to `POST /mcp` and the exact body: one JSON-RPC message, with the bare
tool name. Every answer carries the version of the idp catalog
(`x-queryhub-catalog`). `mcp_door_enabled` closes the door.

This is the Streamable HTTP transport of the MCP spec (2025-06-18), with no
server stream:

- `POST /mcp` takes one JSON-RPC message. A request gets one JSON answer. A
  notification or a response gets `202 Accepted`.
- `GET /mcp` and `DELETE /mcp` answer 405. The server sends no message of its
  own and keeps no MCP session, so it sets no `Mcp-Session-Id`.
- Methods: `ping`, `tools/list` and `tools/call`. The portal answers
  `initialize`.

The door holds no rules. Everything that decides lives behind it: the tools in
`mcp_server.tools`, the submit pipeline, the approval, the executor, the
masking and the audit log.

This router has its own identity and shares nothing with the web routes:

- It lives at `/mcp`, not under `/api`. No web dependency runs here, so
  `own_ui_allowed()` never judges an MCP call. MCP follows Slack: while the
  Slack bot serves a person, this door serves them too. `mcp_door_enabled` is the
  switch that closes it.
- It accepts only an assertion, and refuses a request that also carries a
  token. It finds the person with the same rules and refusal codes as the
  proxied `/api` routes (`deps.assertion_claims`).
- The server decides `origin=mcp` from the tool, never from the client.

The door checks, in this order: the switch, a second credential, the
assertion, the whitelist and the protocol version. A tool call then checks the
rate limit, the free slots and the whitelist (inside
`caller.acting_principal`).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque

import anyio
from anyio import to_thread
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .. import admins, requesters
from .. import config as cfg
from ..mcp_server import caller, catalog, policy, tools
from . import deps, idp_assertion

log = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp")

#: The protocol versions this door speaks, newest first. The portal names one
#: in the MCP-Protocol-Version header.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
#: The JSON-RPC error for a protocol version that the server does not speak.
UNSUPPORTED_PROTOCOL_VERSION = -32022

#: The answer to a call with no assertion. Only the portal calls this door.
PORTAL_ONLY_TEXT = ("QueryHub accepts Claude Code through the portal only. "
                    "In the portal, open QueryHub and select Connect Claude Code.")

#: The version of the idp catalog, on every answer of the door. The portal
#: embeds a copy of the catalog file and compares the two.
CATALOG_HEADER = "x-queryhub-catalog"

#: Tool calls that run at the same time in this process. A call can wait up to
#: `catalog.WAIT_SECONDS` for a result, so this bounds the threads that waiting
#: can hold.
MAX_CONCURRENT_CALLS = 16
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
#: The threads of the door. The web routes share a different pool of 40
#: threads, so calls that wait do not slow the web UI. The 4 extra threads
#: answer `ping`, `tools/list` and the `busy` refusal while 16 calls wait.
DOOR_THREADS = MAX_CONCURRENT_CALLS + 4
_threads: anyio.CapacityLimiter | None = None


def _door_threads() -> anyio.CapacityLimiter:
    """The limiter of the door's threads. The door makes it on first use,
    in the event loop, because an older anyio cannot make one outside a loop."""
    global _threads
    if _threads is None:
        _threads = anyio.CapacityLimiter(DOOR_THREADS)
    return _threads


class _Window:
    """A sliding-window counter per key. In memory and per process, like the
    login throttle: a restart resets it, and the window is a minute."""

    _MAX_KEYS = 20_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: int, seconds: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > self._MAX_KEYS:
                for k in list(self._hits):
                    q = self._hits[k]
                    while q and now - q[0] > seconds:
                        q.popleft()
                    if not q:
                        del self._hits[k]
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


#: The tool calls of each person in the last minute. Keys are `user:...`.
_RATE = _Window()


def _limit_per_minute() -> int:
    try:
        n = cfg.get_int("mcp_rate_limit_per_minute", 60)
    except Exception:  # noqa: BLE001 -- a bad value must not open the door
        return 60
    return n if n > 0 else 60


def connect_info() -> dict | None:
    """What `/api/me` and `/sql claude` say about Claude Code.

    `{"mode": "idp"}` while the door is open and the portal can prove a
    person: the person connects Claude Code to the portal, and the portal
    shows how. None otherwise, so the Welcome page never shows a way that
    does not work.
    """
    try:
        if policy.enabled() and idp_assertion._enabled():
            return {"mode": "idp"}
    except Exception:  # noqa: BLE001 -- /api/me must answer even when this cannot
        return None
    return None


# --- answers ---------------------------------------------------------------


def _ok(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _fail(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _refuse(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    """A refusal before the door reads any message. A JSON-RPC error with no
    id. `data` holds the short QueryHub code."""
    body = _fail(None, -32000, message)
    body["error"]["data"] = {"code": code}
    return JSONResponse(status_code=status, content=body, headers=headers)


def _text(payload: dict, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(jsonable_encoder(payload))}],
            "isError": is_error}


def _tool_error(code: str, message: str) -> dict:
    """A refusal is an answer the model can act on, not a failure of the door."""
    return _text({"error": code, "message": message}, True)


async def _shut() -> JSONResponse | None:
    """The refusal while the MCP door is off. None while it is on."""
    if not await run_in_threadpool(policy.enabled):
        return _refuse(503, "disabled",
                       "Refused: the QueryHub MCP server is off. "
                       "An operator sets `mcp_door_enabled` to `on` to enable it.")
    return None


def _unknown_protocol(request: Request) -> JSONResponse | None:
    """400 for an `MCP-Protocol-Version` header that the server does not speak.
    None for a known version or no header."""
    version = request.headers.get("mcp-protocol-version")
    if not version or version in SUPPORTED_PROTOCOLS:
        return None
    # -32022 with the versions this door speaks, so the portal can tell which
    # version to send.
    body = _fail(None, UNSUPPORTED_PROTOCOL_VERSION,
                 f"QueryHub does not speak MCP {version}. "
                 f"It speaks {', '.join(SUPPORTED_PROTOCOLS)}.")
    body["error"]["data"] = {"supported": list(SUPPORTED_PROTOCOLS), "requested": version}
    return JSONResponse(status_code=400, content=body)


# --- checks ----------------------------------------------------------------


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _admitted(uid: str) -> bool:
    """The whitelist gate: an enabled requester, or an admin. The same gate as
    Slack and the web UI."""
    return admins.is_admin(uid) or requesters.is_allowed(uid)


# --- the transport ---------------------------------------------------------


@router.post("")
async def mcp_post(request: Request):
    """One JSON-RPC message from the portal."""
    answer = await _idp_door(request, request.headers.get(deps.ASSERTION_HEADER) or "")
    answer.headers[CATALOG_HEADER] = catalog.idp_version()
    return answer


async def _idp_door(request: Request, assertion: str) -> Response:
    """The portal, for the person that its assertion names.

    A refusal before the door reads the message (the switch, the credentials,
    the person) is a JSON-RPC error with `data.code`, which the portal turns
    into a tool result. No 401 here names a sign-in: the caller is the
    portal, which cannot start one."""
    shut = await _shut()
    if shut is not None:
        return shut
    if not assertion:
        return _refuse(401, "unauthenticated", "Refused: " + PORTAL_ONLY_TEXT)
    if _bearer(request):
        # Two credentials can name two people, and the door must not pick one
        # of them. The door refuses before it reads the assertion, so its
        # `jti` stays unused.
        return _refuse(400, "ambiguous_credentials",
                       "Refused: the request carries an identity assertion and a "
                       "token. Send only one of them.")
    raw = await request.body()
    try:
        claims = await run_in_threadpool(deps.assertion_claims, assertion, "POST",
                                         deps._request_target(request), raw)
    except deps.AssertionRefused as e:
        log.warning("mcp door=idp refused: %s", e.code)
        # A refusal about the person (no account, outside the domain, an
        # address that two people share) is 403 with its code, so the portal
        # can tell the person what to do. A seam failure is 401 and generic.
        return _refuse(403 if e.person else 401, e.code, e.message)
    uid = claims["sub"]
    if not await run_in_threadpool(_admitted, uid):
        # The resolver also finds a disabled row, and this door has no
        # sign-in that would refuse it.
        log.warning("mcp door=idp refused: not_whitelisted user=%s", uid)
        return _refuse(403, "not_whitelisted",
                       "Refused: you are not whitelisted for QueryHub. "
                       "Ask the DBA team for access.")
    unknown = _unknown_protocol(request)
    if unknown is not None:
        return unknown
    try:
        message = json.loads(raw)
    except ValueError:
        return JSONResponse(status_code=400, content=_fail(None, -32700, "Parse error."))
    if not isinstance(message, dict):
        # One message per request: the portal sends one tool call at a time.
        return JSONResponse(status_code=400, content=_fail(None, -32600, "Invalid request."))
    answer = await to_thread.run_sync(_answer, message, uid, limiter=_door_threads())
    if answer is None:
        return Response(status_code=202)
    return JSONResponse(answer)


@router.get("")
@router.delete("")
def mcp_no_stream():
    """No server stream and no MCP session to end."""
    return Response(status_code=405, headers={"Allow": "POST"})


# --- the methods -----------------------------------------------------------


def _answer(message: dict, uid: str) -> dict | None:
    """The answer to one message. None for a notification or a response."""
    method = message.get("method")
    if method is None:
        return None
    mid = message.get("id")
    if mid is None:
        return None
    if not isinstance(method, str):
        return _fail(mid, -32600, "Invalid request.")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _fail(mid, -32602, "The params must be an object.")
    # The portal answers `initialize` itself, with its own server and its own
    # instructions, so this door does not know the method.
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": catalog.listing()})
    if method == "tools/call":
        return _ok(mid, _call_tool(uid, params.get("name"), params.get("arguments")))
    return _fail(mid, -32601, f"Method not found: {method}")


def _call_tool(uid: str, name, arguments) -> dict:
    """Run one tool as the person that the assertion names."""
    if not isinstance(name, str) or not name:
        return _tool_error("bad_arguments", "The call names no tool.")
    if not _RATE.allow(f"user:{uid}", _limit_per_minute()):
        return _tool_error("rate_limited", "Too many tool calls. Wait a minute, then try again.")
    if not _SLOTS.acquire(blocking=False):
        return _tool_error("busy", "QueryHub is busy. Try again in a moment.")
    started = time.monotonic()
    code = None
    try:
        # The person comes from the assertion. A tool never takes one as an
        # argument, and `acting_as` is the only way this door sets it.
        with caller.acting_as(uid):
            result = catalog.call(name, arguments)
        return _text(result, False)
    except (tools.ToolError, caller.CallerError) as e:
        code = e.code
        return _tool_error(e.code, e.message)
    except Exception:
        code = "server_error"
        log.exception("mcp call failed: door=idp user=%s tool=%s", uid, name)
        return _tool_error("server_error", "QueryHub had an internal error. Try again later.")
    finally:
        _SLOTS.release()
        # The tool and the outcome, never the arguments: a statement can hold
        # data that must not reach a log.
        log.info("mcp call: door=idp user=%s tool=%s outcome=%s ms=%d", uid, name,
                 code or "ok", int((time.monotonic() - started) * 1000))
