"""When a pending request expires: one rule for the expiry job and the web.

`scripts/expire_stale_pending.py --commit` cancels a request that is still
`pending` when its `created_at` is older than `pending_expiry_hours`. It acts
only while `pending_expiry_enabled` is on. The job and the web read the two
keys through this module, so the time that the web shows and the time that the
job uses cannot differ.

`expires_at` is the first moment at which the job can close a request. The job
closes it on its first run after that moment. The operator decides how often
the job runs, so the actual close can come later.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import config as cfg

log = logging.getLogger(__name__)


def enabled() -> bool:
    """True while `pending_expiry_enabled` is on (default off)."""
    return cfg.get_bool("pending_expiry_enabled", False)


def hours() -> int:
    """How long a request can stay pending before the job may close it."""
    return cfg.get_int("pending_expiry_hours", 24)


def window() -> timedelta | None:
    """The time a request can stay pending, or None while expiry is off.

    None also when `pending_expiry_hours` is not a whole number. The job
    stops on such a value, so no request expires, and None says that."""
    if not enabled():
        return None
    try:
        return timedelta(hours=hours())
    except (TypeError, ValueError):
        log.warning("pending_expiry_hours is not a whole number, so QueryHub "
                    "shows no expiry time")
        return None


def expires_at(row: dict, span: timedelta | None) -> datetime | None:
    """When the job may close this request, or None.

    None when expiry is off (`span` is None), when the request is not
    `pending`, or when the row has no `created_at`. The job's own test is
    `created_at < now - span`, so this time is `created_at + span`."""
    if span is None or row.get("status") != "pending":
        return None
    created = row.get("created_at")
    return created + span if created else None
