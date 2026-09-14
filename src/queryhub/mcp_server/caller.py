"""Who a request through this door is being made by.

The one function every tool asks, and the one function that changes when a
chat bot starts fronting this for many people. Keeping it here -- rather than
letting each tool take a "who" argument -- is what makes that later change
small: the tool signatures do not move, and no client can name a person it
picked.

**Today: one principal, from the environment.** The server runs as a local
process for one operator, so the identity is `QH_MCP_PRINCIPAL` rather than a
`bot_config` value. A shared setting would mean every MCP client on the host
acted as the same person, which is wrong even with a single user -- the audit
log would say "somebody" and the grants would be somebody's in particular.

**Later: the person behind a bot.** A bot connects with its own credential and
names the human it is serving. Only this module learns that. What must NOT
change with it: the assertion says WHO and never WHAT. Authority is read from
this service's own tables afterwards, exactly as
`web/idp_assertion.py` already does it for the portal -- a caller may prove an
identity and may never carry a permission.

Whitelisting is the same gate the other two doors apply, deliberately reused
rather than re-expressed: an enabled requester, with admins passing implicitly.
A door that agreed with the others about identity but not about who is admitted
would be a door that widens access by existing.
"""
from __future__ import annotations

import os

from .. import admins, requesters


class CallerError(Exception):
    """No usable caller. `code` is short and safe to log or return."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


ENV_VAR = "QH_MCP_PRINCIPAL"


def acting_principal() -> str:
    """The principal id every tool in this package acts as.

    Raises rather than returning None: a tool that ran with no caller would be
    a tool running with no grants to check, and "no grants" must never be a
    state this code can reach quietly.
    """
    pid = (os.environ.get(ENV_VAR) or "").strip()
    if not pid:
        raise CallerError(
            "no_caller",
            f"{ENV_VAR} is not set. The MCP server acts as one QueryHub user; "
            f"export that user's id in the environment the client is started "
            f"from.")
    if pid.startswith("$"):
        # An editor's MCP config may or may not expand `${VAR}` in the `env`
        # block; Claude Code passes it through literally. The value then
        # arrives as the placeholder text and the whitelist refuses it, which
        # reads as "you are not whitelisted" and sends the reader looking at
        # their QueryHub account instead of their config. Name the actual
        # problem.
        raise CallerError(
            "unexpanded_placeholder",
            f"{ENV_VAR} arrived as the literal text {pid!r}, so whatever set "
            f"it did not expand the variable. Remove the `env` block from the "
            f"MCP client config and export {ENV_VAR} in the environment the "
            f"client itself is started from — the server inherits it.")
    if not (admins.is_admin(pid) or requesters.is_allowed(pid)):
        # Same wording the web gate uses, for the same reason: an id that is
        # merely unknown and one that is disabled are not distinguished, so
        # this cannot be used to probe who exists.
        raise CallerError(
            "not_whitelisted",
            "That user is not whitelisted for QueryHub. Ask the DBA team.")
    return pid


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
