"""`/sql claude`: connect Claude Code to QueryHub.

Claude Code connects through the portal. This command only says how: the
person connects Claude Code in the portal, with a personal access token of the
portal. QueryHub keeps no row for that token, so there is nothing to list or
to end here. The person manages the token in the portal.
"""
from __future__ import annotations

import logging

from .. import admins, requesters
from ..mcp_server import policy

log = logging.getLogger(__name__)

DESCRIPTION = "Connect Claude Code to QueryHub. `/sql claude` says how."

_OFF = (":no_entry: The Claude connection is off. "
        "Ask the DBA team to enable it.")

_USAGE = "Usage: `/sql claude`. Claude Code connects through the portal."

#: The portal page shows the steps for the person's own access token.
_VIA_PORTAL = ("*Connect Claude Code to QueryHub*\n"
               "1. Open QueryHub in the portal.\n"
               "2. Select Connect Claude Code.\n"
               "3. Do the steps on that page. You make a personal access token "
               "and run one command in a terminal.")


def _say(respond, text: str) -> None:
    respond({"response_type": "ephemeral", "text": text})


def handle(user_id, rest, client, respond, body) -> None:
    if not policy.enabled():
        _say(respond, _OFF)
        return
    if not (admins.is_admin(user_id) or requesters.is_allowed(user_id)):
        _say(respond, ":no_entry: Refused: you are not whitelisted for QueryHub. "
                      "Ask the DBA team for access.")
        return
    if (rest or "").split():
        _say(respond, _USAGE)
        return
    _how_to_connect(respond)


def _how_to_connect(respond) -> None:
    # Lazy: the Slack app does not need the web package for anything else.
    from ..web import routes_mcp
    if routes_mcp.connect_info() is None:
        _say(respond, ":x: QueryHub cannot connect Claude Code through the portal yet. "
                      "An admin sets `idp_assertion_enabled` to `on`.")
        return
    _say(respond, _VIA_PORTAL)
