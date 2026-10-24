"""How long an auto-approve rule made in the app may last.

An auto-approve rule skips the review of a query. A rule that never ends skips
it for ever. So every rule that the app creates needs an end date, and the end
cannot be later than `auto_approve_max_days` days from now (default 90, from 1
to 365).

A rule is RO or RW and never DDL: `check_tier` refuses `ddl` with the code
`tier_not_allowed`, in the same places.

ONE function decides the end: `check_end`. Every application path that writes an
auto-approve row calls it:

  - POST /admin/auto-grants and POST /admin/auto-grants/bulk
  - a grant that carries auto-approve (`grants.grant`, `grants.grant_many`,
    POST /admin/grants, and the team branch of that route)
  - POST /admin/grants/fleet with auto-approve
  - copy access with auto-approve (`copied_end`)
  - the approval of a window request (`auto_approve_requests.decide_window`)

tests/test_auto_approve_end_guard.py fails when an INSERT of an auto-approve row
appears in `src/` in a function that does not call this module.

The rule lives in the application only. The database has no CHECK, no trigger
and no NOT NULL on `expires_at` or `valid_until`. A person who inserts a row
with SQL and leaves the end empty gets a rule with no end, and every reader
treats it as permanent. That back door is deliberate (operator decision,
2026-10-24). docs/OPERATIONS.md has the statement and the audit row to write.
Rules that exist now keep their end dates. A rule with no end stays as it is.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import config as cfg

log = logging.getLogger(__name__)

DEFAULT_MAX_DAYS = 90
# The System configuration screen accepts a whole number in this range.
MIN_MAX_DAYS = 1
CEILING_MAX_DAYS = 365

# An end date that a browser computes as "now plus the maximum" arrives a few
# seconds later, and the clocks of the two machines differ a little. The cap
# gives an absolute end this much room. A length in minutes gets none.
CLOCK_SKEW = timedelta(minutes=5)

# An auto-approve rule is RO or RW. Never DDL (operator decision, 2026-10-24).
RULE_TIERS = ("ro", "rw")
TIER_MESSAGE = "An auto-approve rule can be RO or RW. Schema changes always need a DBA."

# The Slack grant modal has no end date field, so it cannot make a rule.
SLACK_NEEDS_END_MESSAGE = (
    "An auto-approve rule needs an end date, and this form has no end date "
    "field. Clear the box and grant the access. Then add the auto-approve in "
    "the web app, under Access, Auto-approve.")


class RuleRefused(ValueError):
    """A new auto-approve rule that the app does not make.

    A ValueError, so a caller that already turns the waiver-tier refusal into
    HTTP 400 turns this one into 400 as well. The web routes use `code` and
    `message` for the body."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class EndRefused(RuleRefused):
    """An end date that an auto-approve rule cannot have.

    `code` is `end_required`, `end_too_far`, `end_in_past` or `bad_request`."""


class TierRefused(RuleRefused):
    """A tier that an auto-approve rule cannot have: `tier_not_allowed`."""


def max_days() -> int:
    """The most days a rule made in the app may last.

    A stored value that is not a whole number from 1 to 365, or a read that
    fails, counts as 90. The limit must not turn into "no limit" when nobody can
    read it."""
    try:
        days = cfg.get_int("auto_approve_max_days", 90)
    except Exception:
        log.warning("could not read auto_approve_max_days. Using %s days",
                    DEFAULT_MAX_DAYS, exc_info=True)
        return DEFAULT_MAX_DAYS
    if not MIN_MAX_DAYS <= days <= CEILING_MAX_DAYS:
        log.warning("auto_approve_max_days is %s, outside %s to %s. Using %s days",
                    days, MIN_MAX_DAYS, CEILING_MAX_DAYS, DEFAULT_MAX_DAYS)
        return DEFAULT_MAX_DAYS
    return days


def check_tier(tier) -> str:
    """The tier of a new rule, checked. Returns it in lower case.

    Only `ro` and `rw`. `ddl`, an unknown tier and a missing one raise
    `TierRefused` with the code `tier_not_allowed`. The check is for rules the
    app makes. A super-admin's own DDL is auto-approved at submit and needs no
    rule. A row that a person inserts with SQL may still have tier `ddl`, and
    every reader honours it."""
    t = str(tier or "").strip().lower()
    if t not in RULE_TIERS:
        raise TierRefused("tier_not_allowed", TIER_MESSAGE)
    return t


def required_message(days: int) -> str:
    return f"An auto-approve rule needs an end date, at most {days} days from now."


def too_far_message(days: int) -> str:
    return (f"The end date is too far away. An auto-approve rule can last at most "
            f"{days} days from now.")


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


def check_end(expires_at=None, *, expires_in_minutes: int | None = None,
              now: datetime | None = None, days: int | None = None) -> datetime:
    """The end of a new rule, checked. Returns the end as an aware datetime.

    `expires_at` is a datetime or an ISO-8601 string. `expires_in_minutes` is
    the other way to say it, and it wins when it is set, as it always did on
    the routes. Raises `EndRefused`:

      end_required  neither is given (or the minutes are zero)
      end_in_past   the end is not ahead of now
      end_too_far   the end is later than now plus `auto_approve_max_days`
      bad_request   a string that is not an ISO-8601 timestamp

    `now` and `days` are for tests. A caller leaves them out.
    """
    days = max_days() if days is None else days
    now = _aware(now) if now is not None else datetime.now(timezone.utc)
    if expires_in_minutes:
        if expires_in_minutes < 0:
            raise EndRefused("end_in_past", "The end date is in the past. Pick a later date.")
        if expires_in_minutes > days * 1440:
            raise EndRefused("end_too_far", too_far_message(days))
        return now + timedelta(minutes=int(expires_in_minutes))
    if expires_at is None or (isinstance(expires_at, str) and not expires_at.strip()):
        raise EndRefused("end_required", required_message(days))
    end = _parse(expires_at)
    if end <= now:
        raise EndRefused("end_in_past", "The end date is in the past. Pick a later date.")
    if end > now + timedelta(days=days) + CLOCK_SKEW:
        raise EndRefused("end_too_far", too_far_message(days))
    return end


def copied_end(source_end, *, now: datetime | None = None,
               days: int | None = None) -> datetime:
    """The end that a COPY of a rule gets: the source's end, but never later
    than now plus `auto_approve_max_days`. A source with no end (a legacy
    permanent rule) gets the cap itself.

    Copy access refuses nothing for this. The admin asked for a copy of what
    the source has, and the copy keeps its shape and loses only the part that a
    new rule cannot have."""
    days = max_days() if days is None else days
    now = _aware(now) if now is not None else datetime.now(timezone.utc)
    cap = now + timedelta(days=days)
    if source_end is None:
        return cap
    return min(_aware(source_end), cap)
