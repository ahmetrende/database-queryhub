"""Lazy email backfill for `requesters` and `admins` rows.

The bot is happy to operate without emails — the DBA inserts users by Slack
ID. But emails are nice for audits / cross-referencing with HR systems, so
the first time a user interacts with the bot we call Slack's users.info,
read the email from their profile, and patch the DB row.

Best-effort: any error (rate-limited, scope missing, no email) is logged at
debug level and ignored.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

try:
    from slack_sdk.errors import SlackApiError
except ModuleNotFoundError:  # vanilla profile: the [slack] extra isn't installed
    class SlackApiError(Exception):  # type: ignore[no-redef]  # sentinel; the lookup path doesn't run here
        pass

if TYPE_CHECKING:  # only a type hint — no runtime dependency on slack_sdk
    from slack_sdk.web import WebClient

from . import db

log = logging.getLogger(__name__)


def lookup_email(slack_user_id: str) -> str | None:
    """Best-effort email lookup across admins + requesters. Used by the
    executor to label connections in `pg_stat_activity` via application_name."""
    row = db.fetch_one(
        "SELECT email FROM admins     WHERE slack_user_id = %s AND email IS NOT NULL "
        "UNION ALL "
        "SELECT email FROM requesters WHERE slack_user_id = %s AND email IS NOT NULL "
        "LIMIT 1",
        (slack_user_id, slack_user_id),
    )
    return row["email"] if row else None


def lookup_tz(slack_user_id: str) -> str | None:
    """IANA timezone for the user (e.g. 'Europe/Istanbul'), or None if not
    yet backfilled. Used to interpret modal schedule-picker values."""
    row = db.fetch_one(
        "SELECT tz FROM admins     WHERE slack_user_id = %s AND tz IS NOT NULL "
        "UNION ALL "
        "SELECT tz FROM requesters WHERE slack_user_id = %s AND tz IS NOT NULL "
        "LIMIT 1",
        (slack_user_id, slack_user_id),
    )
    return row["tz"] if row else None


def maybe_backfill_user_profile(client: WebClient, slack_user_id: str) -> None:
    """Pull name + email + tz from Slack users.info and refresh both the
    `requesters` and `admins` rows for this user. Slack is the source of
    truth — if Slack returns a value we overwrite the DB; if it returns
    nothing for a field, we keep whatever was there. Best-effort: any API
    error is logged at debug level and ignored.

    Called on every /sql submission and every admin button click. Cheap
    enough at the bot's traffic profile to not need caching."""
    if not slack_user_id:
        return
    try:
        info = client.users_info(user=slack_user_id)
    except SlackApiError as e:
        log.debug(
            "users.info failed for %s: %s",
            slack_user_id,
            (e.response or {}).get("error", e),
        )
        return
    user = info.get("user") or {}
    profile = user.get("profile") or {}
    email = profile.get("email")
    name = (
        profile.get("real_name_normalized")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
    )
    tz = user.get("tz")  # IANA name like 'Europe/Istanbul'

    if not (email or name or tz):
        return  # Slack returned nothing useful — leave DB as-is.

    db.execute(
        "UPDATE requesters SET "
        "    email = COALESCE(%s, email), "
        "    tz    = COALESCE(%s, tz), "
        "    name  = COALESCE(%s, name) "
        "WHERE slack_user_id = %s",
        (email, tz, name, slack_user_id),
    )
    db.execute(
        "UPDATE admins SET "
        "    email = COALESCE(%s, email), "
        "    tz    = COALESCE(%s, tz), "
        "    name  = COALESCE(%s, name) "
        "WHERE slack_user_id = %s",
        (email, tz, name, slack_user_id),
    )
    log.info(
        "synced profile for %s (email=%s, tz=%s, name=%s)",
        slack_user_id, bool(email), bool(tz), bool(name),
    )


# Backwards-compatible alias used by older handlers.
maybe_backfill_email = maybe_backfill_user_profile
