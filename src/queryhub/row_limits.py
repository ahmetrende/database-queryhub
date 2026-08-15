"""Per-user result-row-limit overrides (time-bounded).

The global bot_config.max_rows caps how many rows a result file holds;
bot_config.csv_size_mb caps its byte size. Some users need far more for
exports. `set_override` grants a user a higher row cap for a period
(expires_at); `effective_caps` resolves the row + size caps for a given
requester, so the executor uses per-user limits transparently. An expired
or absent override falls back to the global caps — the feature is inert
until an override exists.

The size cap scales with the row cap (up to a hard ceiling) so a genuinely
large export isn't killed by the byte guard the moment the row cap is
raised; the guard stays as a runaway backstop.
"""
from __future__ import annotations

import datetime
import logging

from . import admins
from . import config as cfg
from . import db

log = logging.getLogger(__name__)


def _override_rows(principal_id: str) -> int | None:
    """Active (unexpired) override row cap for a user, or None."""
    row = db.fetch_one(
        "SELECT max_rows FROM user_row_limit_overrides "
        "WHERE slack_user_id = %s "
        "  AND (expires_at IS NULL OR expires_at > now())",
        (principal_id,),
    )
    return row["max_rows"] if row else None


def effective_caps(principal_id: str) -> tuple[int, int]:
    """Return (max_rows, max_csv_bytes) for this requester. An active
    override raises the row cap above the global default and scales the
    size cap with it, bounded by bot_config.csv_size_mb_ceiling."""
    base_rows = cfg.get_int("max_rows", 1000)
    base_mb = cfg.get_int("csv_size_mb", 10)
    ceiling_mb = cfg.get_int("csv_size_mb_ceiling", 100)

    # A super-admin runs without tier gates and without an approver, but NOT
    # without a row cap: the cap is what stops a mistyped SELECT from filling the
    # disk, and that is a resource guard rather than an authorization one. Their
    # floor is raised by config instead of removed, and it defaults to the global
    # cap — so this key is inert until someone sets it deliberately.
    #
    # Read live, like every other authority question here: no cache, and the
    # answer can change between two submissions.
    floor = base_rows
    if admins.is_super_admin(principal_id):
        floor = max(floor, cfg.get_int("super_admin_max_rows", base_rows))

    rows = max(base_rows, floor, _override_rows(principal_id) or 0)
    if rows > base_rows:
        scaled = round(base_mb * rows / max(base_rows, 1))
        mb = min(ceiling_mb, max(base_mb, scaled))
    else:
        mb = base_mb
    return rows, mb * 1024 * 1024


def get_override(principal_id: str) -> dict | None:
    """The user's override row (active or expired), for display/audit."""
    return db.fetch_one(
        "SELECT slack_user_id, max_rows, expires_at, reason, granted_by, "
        "       granted_at, (expires_at IS NOT NULL AND expires_at <= now()) "
        "         AS expired "
        "FROM user_row_limit_overrides WHERE slack_user_id = %s",
        (principal_id,),
    )


def set_override(*, principal_id: str, max_rows: int,
                 expires_at: datetime.datetime | None,
                 reason: str | None, granted_by: str | None) -> None:
    """Grant / update a user's row-limit override. expires_at=None means no
    expiry (discouraged — prefer a period)."""
    db.execute(
        "INSERT INTO user_row_limit_overrides "
        "  (slack_user_id, max_rows, expires_at, reason, granted_by) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (slack_user_id) DO UPDATE SET "
        "  max_rows = EXCLUDED.max_rows, expires_at = EXCLUDED.expires_at, "
        "  reason = EXCLUDED.reason, granted_by = EXCLUDED.granted_by, "
        "  granted_at = now()",
        (principal_id, max_rows, expires_at, reason, granted_by),
    )
    log.info("row-limit override set: %s -> %s rows (expires %s) by %s",
             principal_id, max_rows, expires_at, granted_by)
