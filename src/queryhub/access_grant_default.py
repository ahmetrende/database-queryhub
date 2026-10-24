"""The end date of a grant that an access-request approval writes.

Approving an access request writes a per-user grant. The grant gets an end
date by default: now plus `access_grant_default_days` days (default 90, from 1
to 365). The approver can pick another end, or pick no end.

ONE function decides the end: `approval_end`. Both approval paths call it
through `access_requests.decide`:

  - the web screen: POST /admin/endpoint-requests/{id}/decision
  - the Slack Approve button, which always takes the default

An admin can still give a grant with no end. The grant forms (POST
/admin/grants) are not touched by this module, and the approval screen has a
"No end" choice (`noEnd: true`). Only the DEFAULT is 90 days (operator
decision, 2026-10-24). There is no upper limit on a chosen end.

Grants that exist now keep their end dates. An approval that merges into an
active grant of the same tier also keeps that grant's end date.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import config as cfg

log = logging.getLogger(__name__)

DEFAULT_DAYS = 90
# The System configuration screen accepts a whole number in this range.
MIN_DAYS = 1
CEILING_DAYS = 365

NO_END_AND_END_MESSAGE = "Send an end date or noEnd, not both."


class EndRefused(ValueError):
    """An end date that an approval cannot use.

    A ValueError, so a caller that turns a bad value into HTTP 400 does the
    same here. `code` and `message` are for the web error body."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def default_days() -> int:
    """The default length of a grant that an approval writes, in days.

    A stored value that is not a whole number from 1 to 365, or a read that
    fails, counts as 90. The default must never turn into "no end" when nobody
    can read it."""
    try:
        days = cfg.get_int("access_grant_default_days", DEFAULT_DAYS)
    except Exception:
        log.warning("could not read access_grant_default_days. Using %s days",
                    DEFAULT_DAYS, exc_info=True)
        return DEFAULT_DAYS
    if not MIN_DAYS <= days <= CEILING_DAYS:
        log.warning("access_grant_default_days is %s, outside %s to %s. Using %s days",
                    days, MIN_DAYS, CEILING_DAYS, DEFAULT_DAYS)
        return DEFAULT_DAYS
    return days


def _aware(at: datetime) -> datetime:
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


def _parse(value) -> datetime:
    """An end as a datetime. A string is ISO-8601, read as UTC when it has no
    zone."""
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")))
    except ValueError:
        raise EndRefused("bad_request", "expiresAt must be an ISO-8601 timestamp.")


def approval_end(expires_at=None, *, no_end: bool = False,
                 now: datetime | None = None,
                 days: int | None = None) -> datetime | None:
    """The end of the grant that an approval writes. None means no end.

      no_end            -> None. The approver chose it.
      expires_at given  -> that end. It must be ahead of now. No upper limit.
      neither           -> now plus the default days.

    Raises `EndRefused`:

      bad_request   no_end together with an end, or a string that is not an
                    ISO-8601 timestamp
      end_in_past   the end is not ahead of now

    `now` and `days` are for tests. A caller leaves them out."""
    blank = expires_at is None or (isinstance(expires_at, str) and not expires_at.strip())
    if no_end and not blank:
        raise EndRefused("bad_request", NO_END_AND_END_MESSAGE)
    if no_end:
        return None
    now = _aware(now) if now is not None else datetime.now(timezone.utc)
    if blank:
        days = default_days() if days is None else days
        return now + timedelta(days=days)
    end = _parse(expires_at)
    if end <= now:
        raise EndRefused("end_in_past", "The end date is in the past. Pick a later date.")
    return end
