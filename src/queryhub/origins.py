"""Where a request came from, and how to say it.

`requests.origin` is plain text with a `'slack'` default and no CHECK, so a new
surface can start writing its own value the day it exists. The write was never
the problem. Every READ of it was a two-way branch — `web`, or else Slack — in
four places: the admin DM, the audit feed, and the queue chip twice. A third
surface would therefore have been shown to admins as *Slack*, on the one field
whose entire job is to say which door the request came through.

So the vocabulary lives here, in one place, and an unknown value renders as
itself rather than as one of the two we happened to write first.

`IDP` is declared before anything writes it on purpose: the IdP path (PLA-479)
goes through the same `/api/*` endpoints as the web UI, which means it inherits
`origin='web'` unless it says otherwise. Naming the value here is what stops
two sides inventing two spellings of it.
"""
from __future__ import annotations

SLACK = "slack"
WEB = "web"
IDP = "idp"

# Display names, for the surfaces that show a human which door was used.
_LABELS = {SLACK: "Slack", WEB: "Web", IDP: "IdP"}


def normalize(origin: str | None) -> str:
    """The stored value, lowercased and stripped. Empty falls back to the
    column's own default rather than to an empty string."""
    return (origin or SLACK).strip().lower() or SLACK


def label(origin: str | None) -> str:
    """A name to show. An origin nobody has taught this module about renders as
    itself — visibly unfamiliar, which is the honest answer — never as Slack."""
    key = normalize(origin)
    return _LABELS.get(key, key)


def is_slack(origin: str | None) -> bool:
    """Whether Slack is this request's OWN channel.

    Asked by result delivery, which used to test `!= "web"`. That reads the same
    for today's two values and stops being true the moment a third exists: an
    IdP-origin request would have had its result DM'd into Slack, a surface its
    requester may not be using at all — and, after PLA-479's phase 3, may not
    have."""
    return normalize(origin) == SLACK
