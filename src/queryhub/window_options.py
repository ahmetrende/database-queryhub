"""The lengths of an auto-approve window, and how a length reads to a person.

The Slack modal (`slack_app/ro_window.py`), the web routes and the IdP
outbox (`idp_outbox.py`) use the same list and the same labels. This module
imports nothing, and nothing from `slack_app`. So the web routes and the
outbox work when Slack is off, and also when the `slack_app` package is not
there. `slack_app/ro_window.py` re-exports these names for the Slack code.
"""
from __future__ import annotations

# Selectable window durations (minutes, label). Default is the first that
# matches bot_config.ro_window_minutes (falls back to 60 = 1 hour).
WINDOW_OPTIONS: list[tuple[int, str]] = [
    (60, "1 hour"),
    (180, "3 hours"),
    (480, "8 hours"),
]
_VALID_WINDOW_MINUTES = frozenset(m for m, _ in WINDOW_OPTIONS)


def window_label(minutes: int) -> str:
    """How a window reads to a person. A Slack window reads in hours ("8h").
    A web window of whole days reads in days ("7 days"): 10080 minutes read
    as a unit, not as a number."""
    minutes = int(minutes or 0)
    if minutes and minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} day" + ("" if d == 1 else "s")
    return f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes} min"


def is_valid_window(minutes: int) -> bool:
    return minutes in _VALID_WINDOW_MINUTES
