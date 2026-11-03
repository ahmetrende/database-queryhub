"""What a program is allowed to do through this door, decided in one place.

The first release is read-only, and the intent is that widening it later be a
deliberate act rather than a discovery. Both halves of that are design
decisions, not defaults that happened.

**One ceiling, one function.** Every path that could submit asks `max_tier()`.
Scattering `if tier != "ro"` through the tools is how one of them ends up
missing, and the missing one is the only one that matters.

**Config-backed, not hardcoded.** Every other limit in QueryHub is a
`bot_config` key read per request, so widening this needs no deploy -- which is
also what makes narrowing it again immediate if a program misbehaves. A
constant would have meant a release in both directions.

**Fail closed, twice.** An unset key reads as `ro`, and a key set to something
this module does not recognise reads as `ro` as well, loudly. The second half
matters more than it looks: the sibling bug fixed on 2026-09-08 was a tier
comparison where an unrecognised value ranked ABOVE every ceiling, so a typo
removed the limit instead of the authority. A value nobody can parse must never
be the permissive one.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from .. import config as cfg

log = logging.getLogger(__name__)

# Lowest to highest. The same three the rest of the product uses; this module
# does not invent a fourth.
_TIERS = ("ro", "rw", "ddl")

#: The key an operator sets to widen this door. Absent means `ro`.
CEILING_KEY = "mcp_max_tier"

#: What a program may read of a result. `metadata_only` (the default) sends the
#: status, the row count and the column names. `masked_rows` also sends the
#: masked rows. The rows then reach the model, so the default is the closed one.
RESULT_MODE_KEY = "mcp_result_mode"
_RESULT_MODES = ("metadata_only", "masked_rows")

#: Whether the door answers at all. Absent means off: deploying the door must
#: not be the same act as opening it. A new key on purpose: `mcp_enabled`
#: switched the old stdio server, and an install where it is `on` must not get
#: an open door from a deploy.
ENABLED_KEY = "mcp_door_enabled"


def enabled() -> bool:
    """Whether the MCP surface should serve anything.

    Separate from the ceiling on purpose. "Turn it off" and "turn it down" are
    different operator intents, and during an incident the first one must not
    require reasoning about the second.
    """
    return (cfg.get_setting(ENABLED_KEY, "off") or "off").strip().lower() == "on"


def result_mode() -> str:
    """What a program may read of a result, right now.

    Read per call, like the ceiling, so a change needs no restart. Fail closed:
    an unset, empty or unknown value reads as `metadata_only`. A typo must
    never be the value that sends rows out.
    """
    raw = (cfg.get_setting(RESULT_MODE_KEY, "metadata_only") or "metadata_only").strip().lower()
    if raw not in _RESULT_MODES:
        log.warning("%s is set to %r, which is not one of %s — using 'metadata_only'",
                    RESULT_MODE_KEY, raw, ", ".join(_RESULT_MODES))
        return "metadata_only"
    return raw


def rows_allowed() -> bool:
    """Whether result rows may reach the program."""
    return result_mode() == "masked_rows"


def portal_base() -> str | None:
    """The address of QueryHub in the portal, from `mcp_portal_url`, with no
    trailing slash. None while the setting is empty or not an absolute http(s)
    address with no user part and no fragment.

    The portal is the only web UI of the MCP door: the person opens a request
    at `<address>?request=<id>`.
    """
    raw = (cfg.get_setting("mcp_portal_url", "") or "").strip().rstrip("/")
    parts = urlsplit(raw)
    if (parts.scheme in ("http", "https") and parts.netloc and not parts.fragment
            and not parts.query and not parts.username and not parts.password):
        return raw
    return None


def request_url(request_id: int) -> str | None:
    """The link to one request in the portal, or None when the portal address
    is unknown.

    The link opens the request as a tab of the person's workspace in the
    portal. It goes back to the person, who may be an approver, so they can
    act on it. It carries no secret and no identity: the portal signs the
    person in, and QueryHub shows a request only to its owner.
    """
    base = portal_base()
    if base is None:
        return None
    return f"{base}?request={int(request_id)}"


def max_tier() -> str:
    """The highest tier a program may submit, right now.

    Read per call, like every other limit here, so an operator narrowing this
    during an incident does not wait for a restart.
    """
    raw = (cfg.get_setting(CEILING_KEY, "ro") or "ro").strip().lower()
    if raw not in _TIERS:
        # Loud, because a typo here is silent otherwise and the failure it
        # would cause is a widening.
        log.warning("%s is set to %r, which is not one of %s — using 'ro'",
                    CEILING_KEY, raw, ", ".join(_TIERS))
        return "ro"
    return raw


def tier_allowed(required: str) -> bool:
    """Whether a statement classified as `required` may be submitted.

    An unrecognised requirement is refused rather than compared. The classifier
    only ever returns one of the three, so reaching this means something
    upstream changed, and a tier this module cannot rank is not one it can
    say is low enough.
    """
    req = (required or "").strip().lower()
    if req not in _TIERS:
        log.warning("refusing an unrecognised required tier: %r", required)
        return False
    return _TIERS.index(req) <= _TIERS.index(max_tier())


def refusal(required: str) -> str:
    """What to tell the caller, in terms it can act on.

    Names the tier that was needed and the tier that is allowed, because "not
    permitted" sends an assistant into retrying the same statement.
    """
    return (f"This MCP server accepts statements up to tier "
            f"{max_tier().upper()}. This statement needs tier "
            f"{(required or '?').upper()}. Submit it from Slack or the web UI "
            f"instead.")
