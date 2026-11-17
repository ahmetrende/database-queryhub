"""Who can use QueryHub's own Slack entry: `/sql` and the modals it opens.

With bot_config `slack_own_ui_super_admin_only` on, only a super-admin or a
person on `slack_own_ui_allowed_users` can run `/sql` (the modal and every
subcommand), submit a `/sql` modal or open a request again from its message.
Everyone else gets a notice with a link to the portal. The link is
`web_portal_url`, the same address that the closed web UI sends people to.

This is the Slack copy of the web gate (`web.deps.own_ui_allowed`). The two
switches and lists are separate, so each surface can open or close alone.

The gate does not close what Slack does for a request made somewhere else:
the approval buttons, the result and status messages, the ratings and the
access-request decisions stay open.

The gate closes on a fault. When a key or the admin rows cannot be read,
`is_open` returns False and the person gets the notice.
"""
from __future__ import annotations

import logging

from .. import admins
from .. import config as cfg
from ..web.portal_redirect import portal_url

log = logging.getLogger(__name__)

SWITCH = "slack_own_ui_super_admin_only"
ALLOWED_USERS = "slack_own_ui_allowed_users"

_ON = {"on", "1", "true", "yes"}


def _allowed_users() -> set[str]:
    raw = cfg.get_setting(ALLOWED_USERS, "") or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def is_open(user_id: str | None) -> bool:
    """True when this person can use the Slack entry.

    Always True while the switch is off. With the switch on: True for a
    super-admin and for a person on the allowed list, False for everyone
    else and on any read fault.
    """
    try:
        val = (cfg.get_setting(SWITCH, "off") or "").strip().lower()
        if val not in _ON:
            return True
        if user_id and user_id in _allowed_users():
            return True
        return bool(user_id) and admins.is_super_admin(user_id)
    except Exception:
        log.exception("slack entry gate: read failed for %s; refusing", user_id)
        return False


def notice_text() -> str:
    """The notice for a refused person, in Slack mrkdwn."""
    url = portal_url()
    head = ":no_entry: *You cannot use QueryHub in Slack.*\nQueryHub opens in the portal."
    if url:
        return f"{head} Go to <{url}|QueryHub in the portal> to run a query."
    return f"{head} Ask the DBA team for the portal address."


def notice_blocks() -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": notice_text()}}]


def ephemeral() -> dict:
    """A `respond()` payload for a slash command."""
    return {"response_type": "ephemeral", "text": notice_text(),
            "blocks": notice_blocks()}


def notice_view() -> dict:
    """A modal with the notice, for a modal submit or a button."""
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "QueryHub"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": notice_blocks(),
    }
