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

from .. import config as cfg

log = logging.getLogger(__name__)

# Lowest to highest. The same three the rest of the product uses; this module
# does not invent a fourth.
_TIERS = ("ro", "rw", "ddl")

#: The key an operator sets to widen this door. Absent means `ro`.
CEILING_KEY = "mcp_max_tier"

#: Whether the door answers at all. Absent means off: installing the extra
#: dependency must not be the same act as opening the door.
ENABLED_KEY = "mcp_enabled"


def enabled() -> bool:
    """Whether the MCP surface should serve anything.

    Separate from the ceiling on purpose. "Turn it off" and "turn it down" are
    different operator intents, and during an incident the first one must not
    require reasoning about the second.
    """
    return (cfg.get_setting(ENABLED_KEY, "off") or "off").strip().lower() == "on"


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
    return (f"This connection accepts up to {max_tier().upper()} statements; "
            f"that one needs {(required or '?').upper()}. Submit it from Slack "
            f"or the web UI instead.")
