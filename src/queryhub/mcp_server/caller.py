"""Who a call through the MCP door is being made by.

The one function every tool asks. No tool takes a "who" argument, so no
client can name a person it picked.

The door binds the person for one call with `acting_as`: the person that the
portal proves with its identity assertion. The assertion says WHO and never
WHAT. Authority is read from this service's own tables afterwards, exactly as
`web/idp_assertion.py` does it for the proxied `/api` routes. A caller may
prove an identity and may never carry a permission.

Whitelisting is the same gate as Slack and the web UI, deliberately reused
rather than re-expressed: an enabled requester, with admins passing implicitly.
A door that agreed with the others about identity but not about who is admitted
would be a door that widens access by existing.

No other source of a person exists. A tool that runs outside a bound call
raises `no_caller`.
"""
from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator

from .. import admins, requesters


class CallerError(Exception):
    """No usable caller. `code` is short and safe to log or return."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# The person behind the call being served right now, set by the door for the
# length of one call. It is a context variable, so two calls on two threads
# never see each other's person.
_BOUND: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "qh_mcp_bound_principal", default=None)


@contextlib.contextmanager
def acting_as(principal_id: str) -> Iterator[None]:
    """Run one call as `principal_id`, who the portal already proved.

    The HTTP door calls this for every tool call, with the person that the
    portal's assertion names. The person is never a tool argument. An empty id raises, because a
    call with no person would have no grants to check.
    """
    pid = (principal_id or "").strip()
    if not pid:
        raise CallerError("no_caller", "Refused: the call names no person.")
    token = _BOUND.set(pid)
    try:
        yield
    finally:
        _BOUND.reset(token)


def acting_principal() -> str:
    """The principal id every tool in this package acts as.

    Raises rather than returning None: a tool that ran with no caller would be
    a tool running with no grants to check, and "no grants" must never be a
    state this code can reach quietly.
    """
    bound = _BOUND.get()
    if bound is None:
        raise CallerError("no_caller", "Refused: the call names no person.")
    # The same whitelist gate as every other door, asked on every call. A
    # person who lost access stops working at once.
    if not (admins.is_admin(bound) or requesters.is_allowed(bound)):
        raise CallerError(
            "not_whitelisted",
            "Refused: you are not whitelisted for QueryHub. "
            "Ask the DBA team for access.")
    return bound


def display_name(principal_id: str) -> str:
    """A name for the audit trail and admin notifications.

    Falls back to the id. Every surface that shows a request shows a name, and
    a blank one there reads as a bug in the surface rather than a gap in the
    row.
    """
    from .. import db
    row = db.fetch_one(
        "SELECT name FROM requesters WHERE slack_user_id = %s "
        "UNION ALL SELECT name FROM admins WHERE slack_user_id = %s "
        "LIMIT 1", (principal_id, principal_id))
    return (row or {}).get("name") or principal_id
