"""A sync that would disable many requesters waits for a super-admin.

The IDP panel sends the full list of people who may use QueryHub every fifteen
minutes, and the reconcile (`POST /api/admin/principals/sync`) disables whoever
is missing. A wrong list, such as a role not yet granted or one removed by
mistake, would lock that many people out of Slack, the web and MCP within one
tick. So a run that would disable more than `idp_sync_max_disable` requesters
(default 5) is held: nothing changes, the panel's job gets a 409, and every
super-admin gets one Slack card asking whether they approve.

- Approving covers the people on the card, once, for 24 hours. The next run
  whose disable list lies inside that set is applied and stamps `applied_at`.
- Rejecting keeps the same list blocked for as long as it keeps arriving.
- A different list is a new question, and the older card is superseded.
- The same undecided list is asked about again after 24 hours, in case the
  first card was missed.

The whole run is held, enables included: a list that looks wrong is not applied
in part. Nothing here runs unless the sync principal calls the reconcile, which
needs `idp_assertion_enabled`. Rows live in `idp_sync_hold` (migration 138).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from . import admins, audit, db
from . import config as cfg

log = logging.getLogger(__name__)

LIMIT_KEY = "idp_sync_max_disable"
DEFAULT_LIMIT = 5

# The same 24 hours does three jobs: how long an approval stays usable, how long
# a list may go unseen before it counts as a new incident, and the gap between
# reminders about a list nobody has decided.
_ONE_DAY = "INTERVAL '24 hours'"

# An approval that can still be used for this list: unspent, recent, and naming
# at least everyone the run would disable.
_USABLE_APPROVAL = (
    "status = 'approved' AND applied_at IS NULL "
    f"AND decided_at > NOW() - {_ONE_DAY} "
    "AND would_disable @> %s::text[]")


class HoldDecisionRefused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message, self.status = message, status


@dataclass(frozen=True)
class Verdict:
    """What the guard decided about one run.

    `state` is within_limit, approved (a super-admin said yes) or pending
    (waiting for one), rejected, or, for a dry run only, would_hold (this list
    would be held and a card sent). Only the first two let the run apply.
    """
    state: str
    limit: int
    count: int
    hold_id: int | None = None
    notified: int = 0

    @property
    def allowed(self) -> bool:
        return self.state in ("within_limit", "approved")

    def as_dict(self) -> dict:
        out = {"state": self.state, "limit": self.limit, "would_disable": self.count}
        if self.hold_id is not None:
            out["hold_id"] = self.hold_id
        return out

    def message(self) -> str:
        head = (f"This sync would disable {self.count} requesters, more than the "
                f"limit of {self.limit}.")
        if self.state == "rejected":
            return (f"{head} A super-admin rejected it. Nothing was changed. Fix the "
                    f"panel's list, or raise {LIMIT_KEY}.")
        return f"{head} A super-admin has been asked to approve it. Nothing was changed."


def limit() -> int:
    """The most requesters one sync may disable without a yes.

    A value that is not a whole number, or is negative, falls back to the default
    rather than switching the guard off. Zero is allowed: every disabling run
    then needs approval.
    """
    try:
        n = cfg.get_int(LIMIT_KEY, DEFAULT_LIMIT)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return n if n >= 0 else DEFAULT_LIMIT


def _clean(ids) -> list[str]:
    return sorted(set(ids))


def get(hold_id: int) -> dict | None:
    return db.fetch_one(
        "SELECT id, would_disable, limit_at_hold, status, cards, first_held_at, "
        "       last_held_at, last_notified_at, decided_by_slack_id, decided_at, "
        "       applied_at "
        "  FROM idp_sync_hold WHERE id = %s", (hold_id,))


def _usable_approval(ids: list[str]) -> dict | None:
    return db.fetch_one(
        f"SELECT id FROM idp_sync_hold WHERE {_USABLE_APPROVAL} "
        " ORDER BY decided_at DESC LIMIT 1", (ids,))


def _claim_approval(ids: list[str]) -> dict | None:
    """Spend a usable approval. The claim is one statement, so two overlapping
    runs cannot both use it."""
    return db.fetch_one(
        "UPDATE idp_sync_hold SET applied_at = NOW() "
        " WHERE id = (SELECT id FROM idp_sync_hold "
        f"             WHERE {_USABLE_APPROVAL} "
        "              ORDER BY decided_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED) "
        "   AND applied_at IS NULL "
        "RETURNING id", (ids,))


def _same_list(ids: list[str]) -> dict | None:
    """The undecided or rejected hold for exactly this list, if it was seen in
    the last day. `reminder_due` says its card has gone a day unanswered."""
    return db.fetch_one(
        "SELECT id, status, "
        f"       (last_notified_at IS NULL OR last_notified_at < NOW() - {_ONE_DAY}) "
        "         AS reminder_due "
        "  FROM idp_sync_hold "
        " WHERE status IN ('pending', 'rejected') "
        "   AND would_disable @> %s::text[] AND would_disable <@ %s::text[] "
        f"   AND last_held_at > NOW() - {_ONE_DAY} "
        " ORDER BY id DESC LIMIT 1", (ids, ids))


def _open(ids: list[str], lim: int, actor: str | None) -> dict:
    """Record a new held list. Any older undecided list is superseded: its card
    is about people who may no longer be the question."""
    with db.transaction() as cur:
        cur.execute("UPDATE idp_sync_hold SET status = 'superseded' "
                    " WHERE status = 'pending'")
        cur.execute(
            "INSERT INTO idp_sync_hold (would_disable, limit_at_hold) "
            "VALUES (%s, %s) RETURNING id", (ids, lim))
        row = cur.fetchone()
        audit.log_in(cur, None, actor, "idp-sync", "idp_principal_sync_held",
                     {"hold_id": row["id"], "would_disable": ids, "limit": lim})
    return row


def _client():
    """The Slack client of whichever process runs the reconcile."""
    if not cfg.ENV.slack_enabled:
        return None
    from slack_sdk import WebClient
    return WebClient(token=cfg.ENV.slack_bot_token)


def _notify(hold_id: int) -> int:
    """DM every super-admin the approve / reject card. Returns how many were
    reached; zero means nobody can decide it, and the run simply stays held."""
    client = _client()
    hold = get(hold_id)
    if client is None or hold is None:
        log.warning("idp sync hold %s: no Slack client, so no card was sent", hold_id)
        return 0
    from .slack_app import idp_sync_card, notifications
    blocks = idp_sync_card.blocks(hold)
    fallback = idp_sync_card.fallback(hold)
    cards = []
    for a in admins.list_active():
        uid = a["slack_user_id"]
        if not admins.is_super_admin(uid):
            continue
        try:
            opened = client.conversations_open(users=uid)
            channel = opened["channel"]["id"]
            posted = notifications._post(client, channel=channel, text=fallback,
                                         blocks=blocks)
            cards.append({"user": uid, "channel": channel, "ts": posted["ts"]})
        except Exception:
            log.exception("idp sync hold %s: card to %s failed", hold_id, uid)
    # Appended, so a reminder does not forget where the first cards went.
    db.execute("UPDATE idp_sync_hold SET cards = cards || %s::jsonb, "
               "       last_notified_at = NOW() WHERE id = %s",
               (json.dumps(cards), hold_id))
    if not cards:
        log.warning("idp sync hold %s: no super-admin could be reached", hold_id)
    return len(cards)


def preview(disabled_ids) -> Verdict:
    """What `gate` would say, changing nothing: for a dry run."""
    ids, lim = _clean(disabled_ids), limit()
    if len(ids) <= lim:
        return Verdict("within_limit", lim, len(ids))
    usable = _usable_approval(ids)
    if usable:
        return Verdict("approved", lim, len(ids), usable["id"])
    held = _same_list(ids)
    if held:
        return Verdict(held["status"], lim, len(ids), held["id"])
    return Verdict("would_hold", lim, len(ids))


def gate(disabled_ids, *, actor: str | None = None) -> Verdict:
    """Decide whether a run that would disable these requesters may apply.

    Called once per run, before anything is written. `actor` is the sync
    principal, for the audit trail.
    """
    ids, lim = _clean(disabled_ids), limit()
    if len(ids) <= lim:
        return Verdict("within_limit", lim, len(ids))
    spent = _claim_approval(ids)
    if spent:
        return Verdict("approved", lim, len(ids), spent["id"])
    held = _same_list(ids)
    if held is None:
        opened = _open(ids, lim, actor)
        return Verdict("pending", lim, len(ids), opened["id"],
                       notified=_notify(opened["id"]))
    db.execute("UPDATE idp_sync_hold SET last_held_at = NOW() WHERE id = %s",
               (held["id"],))
    if held["status"] == "pending" and held["reminder_due"]:
        return Verdict("pending", lim, len(ids), held["id"],
                       notified=_notify(held["id"]))
    return Verdict(held["status"], lim, len(ids), held["id"])


def decide(hold_id: int, *, approve: bool, actor_id: str,
           actor_name: str | None) -> dict:
    """Approve or reject a held sync: the Slack card's one rulebook.

    Only a super-admin may. Two deciding at once: the second finds it already
    decided. Returns the row, with the cards to close.
    """
    if not admins.is_super_admin(actor_id):
        raise HoldDecisionRefused("Super-admin access required.", 403)
    status = "approved" if approve else "rejected"
    row: dict | None = None
    with db.transaction() as cur:
        cur.execute(
            "UPDATE idp_sync_hold SET status = %s, decided_by_slack_id = %s, "
            "       decided_by_name = %s, decided_at = NOW() "
            " WHERE id = %s AND status = 'pending' "
            "RETURNING id, would_disable, limit_at_hold, cards, status",
            (status, actor_id, actor_name, hold_id))
        row = cur.fetchone()
        if row is not None:
            audit.log_in(cur, None, actor_id, actor_name, f"idp_sync_hold_{status}",
                         {"hold_id": hold_id, "would_disable": row["would_disable"]})
    if row is None:
        current = get(hold_id)
        if current is None:
            raise HoldDecisionRefused("No such request.", 404)
        raise HoldDecisionRefused(f"Already {current['status']}.")
    return row
