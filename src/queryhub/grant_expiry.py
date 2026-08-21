"""Warn a grant holder before a time-bounded grant lapses.

Migration 096 let a standing grant end on its own; nothing announced it. The
auth-event triggers fire on row CHANGES and time passing is not one, so access
stopped working and the holder found out by being refused. Renewal is the case
that matters: an offboarding SHOULD go quiet, a grant someone still needs
should not.

Two thresholds, because they answer different questions — a day out is
"arrange the renewal", four hours out is "you are about to lose this mid-task".

Auto-approve windows are deliberately out of scope: they are short by design
and requested with a duration in mind, so a four-hour warning about a one-hour
window is noise, and losing one costs an approval rather than access.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from slack_sdk import WebClient

from . import config as cfg
from . import db

log = logging.getLogger(__name__)


def is_enabled() -> bool:
    return (cfg.get_setting("grant_expiry_warn_enabled", "on") or "on") \
        .strip().lower() in {"on", "true", "yes", "1"}


def thresholds() -> list[int]:
    """Hours-before-expiry to warn at, NARROWEST first.

    Each threshold owns a half-open bucket bounded by the next one down, so a
    grant belongs to exactly one of them at any moment: with 24 and 4
    configured, 4 means "0-4 hours left" and 24 means "4-24 hours left".

    That disjointness is the whole point. Treating a threshold as "anything
    closer than h" meant a grant created with three hours left qualified for
    BOTH, and the 24-hour message — which reads "expires tomorrow" — went out
    about a grant expiring the same afternoon. Buckets make each warning true
    when it is sent.
    """
    raw = cfg.get_setting("grant_expiry_warn_hours", "24,4") or ""
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h = int(part)
        except ValueError:
            log.warning("grant_expiry_warn_hours: ignoring %r", part)
            continue
        if h > 0:
            out.append(h)
    return sorted(set(out))


def _fmt_left(delta: timedelta) -> str:
    """How long is actually left, in the coarsest honest unit.

    Read from the deadline rather than from the threshold that triggered the
    warning. The threshold is a trigger, not a fact: describing a grant by the
    bucket it fell into is how "expires tomorrow" was sent about a grant with
    three hours left.
    """
    mins = max(0, int(delta.total_seconds() // 60))
    if mins < 60:
        return f"in {mins} minute{'s' if mins != 1 else ''}"
    hours = mins // 60
    if hours < 24:
        return f"in {hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return "tomorrow" if days == 1 else f"in {days} days"


def due(threshold_hours: int, floor_hours: int = 0) -> list[dict]:
    """Grants in the bucket `(floor_hours, threshold_hours]` before expiry that
    have not been warned at this threshold for this deadline.

    `floor_hours` is the next narrower threshold, which is what keeps the
    buckets disjoint: without it every threshold matched everything closer than
    itself, so a grant with three hours left qualified for the 24-hour warning
    too and was told it expired tomorrow.

    A grant already past its expiry is skipped — the warning would arrive after
    the access it described had gone, which is worse than silence.
    """
    rows = db.fetch_all(
        """
        SELECT 'user' AS kind, g.id, g.slack_user_id AS subject, g.mode,
               g.expires_at, t.alias, NULL::text AS team_name
          FROM user_target_grants g
          JOIN target_servers t ON t.id = g.target_server_id
         WHERE g.revoked_at IS NULL
           AND g.expires_at IS NOT NULL
           AND g.expires_at > NOW() + make_interval(hours => %(lo)s)
           AND g.expires_at <= NOW() + make_interval(hours => %(h)s)
           AND NOT EXISTS (SELECT 1 FROM grant_expiry_notices n
                            WHERE n.grant_kind = 'user' AND n.grant_id = g.id
                              AND n.threshold_hours = %(h)s
                              AND n.expires_at = g.expires_at)
        UNION ALL
        SELECT 'team', g.id, NULL, g.mode, g.expires_at, t.alias, tm.name
          FROM team_target_grants g
          JOIN target_servers t ON t.id = g.target_server_id
          JOIN teams tm ON tm.id = g.team_id
         WHERE g.revoked_at IS NULL
           AND g.expires_at IS NOT NULL
           AND g.expires_at > NOW() + make_interval(hours => %(lo)s)
           AND g.expires_at <= NOW() + make_interval(hours => %(h)s)
           AND NOT EXISTS (SELECT 1 FROM grant_expiry_notices n
                            WHERE n.grant_kind = 'team' AND n.grant_id = g.id
                              AND n.threshold_hours = %(h)s
                              AND n.expires_at = g.expires_at)
        ORDER BY expires_at
        """,
        {"h": threshold_hours, "lo": floor_hours})
    return rows


def recipients_for(grant: dict) -> list[str]:
    """Who loses access when this grant lapses.

    A team grant is warned to every member — the grant is what gives them the
    access, so they are the ones who will be refused.
    """
    if grant["kind"] == "user":
        return [grant["subject"]] if grant["subject"] else []
    rows = db.fetch_all(
        "SELECT m.slack_user_id FROM team_members m "
        "  JOIN team_target_grants g ON g.team_id = m.team_id "
        " WHERE g.id = %s", (grant["id"],))
    return [r["slack_user_id"] for r in rows]


def message(grant: dict, now: datetime | None = None) -> str:
    tier = (grant["mode"] or "ro").upper()
    now = now or datetime.now(timezone.utc)
    when = _fmt_left(grant["expires_at"] - now)
    stamp = grant["expires_at"].strftime("%Y-%m-%d %H:%M UTC")
    via = (f" (through the *{grant['team_name']}* team)"
           if grant["kind"] == "team" else "")
    return (f":hourglass: Your *{tier}* access to `{grant['alias']}`{via} "
            f"expires {when} — {stamp}. Queries stop being accepted after "
            "that. Ask an admin to extend it if you still need it.")


def sweep(client: WebClient) -> int:
    """Send any due warnings. Returns how many grants were warned about.

    The notice is recorded only AFTER at least one DM has landed, which chooses
    duplicates over silence: a crash between sending and recording repeats a
    warning on the next pass, while recording first would swallow it entirely.
    A warning arriving twice is mildly annoying; access disappearing with no
    warning is the failure this exists to prevent.
    """
    if not is_enabled():
        return 0
    from .slack_app import notifications
    warned = 0
    # Ascending, each threshold floored by the previous one, so the buckets are
    # disjoint and a grant is in exactly one. No cross-threshold suppression is
    # needed — nor wanted: a long-lived grant SHOULD get the 24-hour warning
    # and then the 4-hour one as it crosses each line.
    floor = 0
    for h in thresholds():
        for g in due(h, floor):
            people = recipients_for(g)
            if not people:
                # Nobody to tell (an empty team). Record it so the sweep does
                # not re-query this grant every minute until it expires.
                _record(g, h, 0)
                continue
            text = message(g)
            ok = 0
            for uid in people:
                try:
                    notifications.dm_requester(client, uid, text)
                    ok += 1
                except Exception:
                    log.exception("grant_expiry: DM to %s failed", uid)
            if ok:
                _record(g, h, ok)
                warned += 1
        floor = h
    return warned


def _record(grant: dict, threshold_hours: int, recipients: int) -> None:
    db.execute(
        "INSERT INTO grant_expiry_notices "
        "  (grant_kind, grant_id, threshold_hours, expires_at, recipients) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (grant_kind, grant_id, threshold_hours, expires_at) "
        "  DO NOTHING",
        (grant["kind"], grant["id"], threshold_hours,
         grant["expires_at"], recipients))
