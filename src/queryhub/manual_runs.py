"""Requests the bot handed to a DBA to run by hand (status awaiting_dba_manual).

The executor moves a DDL request here when the bot's own role is not allowed
to run it -- `permission denied to create role`, `must be owner`, a statement
that cannot run inside the transaction a multi-statement request needs. From
then on only a person can finish it: they run the SQL out-of-band and mark the
request completed, or mark it failed with a reason.

The Slack buttons and the web admin panel both close requests through
`close()`, so the row transition and its audit entry exist once. They used to
live in the two Slack handlers only, which left the web with no way out of
this state at all.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import admins, audit, db

log = logging.getLogger(__name__)

STATUS = "awaiting_dba_manual"


@dataclass
class Closed:
    """A request that `close()` moved out of awaiting_dba_manual."""
    row: dict
    completed: bool
    reason: str | None
    actor_ref: str


def actor_ref(actor_id: str | None, actor_name: str | None) -> str:
    """How the closer is named in the stored note and in Slack.

    A Slack user id becomes a mention, which Slack renders as the person's
    name. A web-only account (`local:<name>`) has no Slack user to mention, and
    `<@local:x>` would print as literal text, so it is named instead.
    """
    if actor_id and ":" not in actor_id:
        return f"<@{actor_id}>"
    return actor_name or actor_id or "an admin"


def close(request_id: int, *, completed: bool, actor_id: str | None,
          actor_name: str | None, reason: str | None = None,
          returning: str = "*") -> Closed | None:
    """Mark an escalated request completed or failed.

    Returns None when the request is not waiting any more -- someone else
    closed it first, or it was never escalated -- so a double click, or the
    Slack button and the web panel racing each other, closes it once.
    """
    ref = actor_ref(actor_id, actor_name)
    reason = (reason or "").strip() or None
    with db.transaction() as cur:
        if completed:
            note = f"manually completed by {ref}"
            cur.execute(
                "UPDATE requests SET status = 'completed', completed_at = NOW(), "
                " decision_reason = CASE WHEN decision_reason IS NULL "
                "                          OR decision_reason = '' THEN %s "
                "                        ELSE decision_reason || ' / ' || %s END "
                "WHERE id = %s AND status = 'awaiting_dba_manual' "
                f"RETURNING {returning}",
                (note, note, request_id))
        else:
            cur.execute(
                "UPDATE requests SET status = 'failed', completed_at = NOW(), "
                " error_message = %s "
                "WHERE id = %s AND status = 'awaiting_dba_manual' "
                f"RETURNING {returning}",
                (f"manual DBA execution failed: {reason or '(no reason given)'}",
                 request_id))
        row = cur.fetchone()
        if row is None:
            return None
        if completed:
            audit.log_in(cur, request_id, actor_id, actor_name,
                         "completed_manually")
        else:
            audit.log_in(cur, request_id, actor_id, actor_name,
                         "failed_manually",
                         {"reason": reason or "(no reason given)"})
    return Closed(row=dict(row), completed=completed, reason=reason,
                  actor_ref=ref)


def list_open_for(admin_id: str) -> list[dict]:
    """Escalated requests this admin could close, oldest first.

    The same authority as the buttons: `admins.can_approve` on the request, so
    a pod lead sees their pod's and a full admin sees all of them.
    """
    rows = db.fetch_all(
        "SELECT r.id, r.requester_slack_id, r.requester_name, "
        "       r.target_server_id, r.database_name, r.query, "
        "       r.required_tier, r.engine, r.error_message, r.created_at, "
        "       r.executed_at, r.bundle_id, t.alias AS target_alias "
        "  FROM requests r JOIN target_servers t ON t.id = r.target_server_id "
        " WHERE r.status = 'awaiting_dba_manual' "
        " ORDER BY r.id")
    return [r for r in rows if admins.can_approve(admin_id, r)]


def notify_closed(client, closed: Closed) -> None:
    """The Slack side of a close-out: collapse the admin cards and tell the
    requester. No-op without a Slack client (the web in the vanilla profile).
    """
    if client is None:
        return
    from . import ratings
    from .slack_app import notifications
    row = closed.row
    rid = row["id"]
    if closed.completed:
        line = (f":white_check_mark: Manually completed by {closed.actor_ref} "
                f"(DDL ran out-of-band).")
    else:
        line = (f":x: Marked failed by {closed.actor_ref} after manual attempt "
                f"— {closed.reason or '(no reason given)'}")
    # A bundle item's approval card is the bundle's; a card posted because the
    # request was auto-approved and had none is keyed to the request itself.
    # Update both -- whichever does not exist is a no-op.
    if row.get("bundle_id"):
        notifications.update_bundle_admin_dms(client, row["bundle_id"])
    notifications.update_all_admin_messages(client, row, line)
    if row.get("bundle_id"):
        return  # the bundle summary covers the requester
    if closed.completed:
        notifications.dm_requester(
            client, row["requester_slack_id"],
            f":white_check_mark: *SQL query `#{rid}` completed* — "
            f"DBA ran it manually with elevated credentials.\n"
            + notifications.request_context_md(row),
        )
    else:
        notifications.dm_requester(
            client, row["requester_slack_id"],
            f":x: *SQL query `#{rid}` failed* during DBA manual "
            f"execution.\n"
            + notifications.request_context_with_query_md(row)
            + f"\n*Reason:* {closed.reason or '(no reason given)'}",
        )
    try:
        ratings.maybe_prompt(client, row)
    except Exception:
        log.exception("rating prompt failed for request %s", rid)
