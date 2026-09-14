"""Transport-agnostic approve / reject / request-changes pipeline.

Extracted from the Slack decision handlers so the web admin panel and the
Slack approval buttons share ONE implementation of the decision path —
the DB transition + audit, and the post-decision effects (Slack mirror +
executor dispatch). Per ADMIN_API.md a web decision must mirror into Slack
(update the approval message, notify the submitter) — it does, by reusing
the exact same notification/dispatch code both surfaces run.

    decide(request_id, decision, by_id, by_name, reason) -> Outcome | None
        atomic pending→(approved|scheduled|rejected|changes_requested)
        UPDATE + audit row. None if already decided (raced).
    apply_effects(client, outcome)
        Slack mirror (admin messages + requester card) + executor.submit
        on approve. Safe to call from either process.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from . import audit, core_submit, db

_VALID = ("approve", "reject", "changes")


@dataclass
class Outcome:
    row: dict
    decision: str          # approve | reject | changes
    deferred: bool         # approve of a future-scheduled request → 'scheduled'


def decide(request_id: int, decision: str, *, by_id: str,
           by_name: str | None, reason: str | None = None) -> Outcome | None:
    """Atomically move a pending request to its decided state + write the
    audit row. Returns None if the request wasn't pending (already decided
    / raced). `reason` is required for reject/changes."""
    if decision not in _VALID:
        raise ValueError(f"decision must be one of {_VALID}, got {decision!r}")
    if decision in ("reject", "changes") and not (reason or "").strip():
        raise ValueError(f"{decision} requires a reason")

    ret = core_submit.REQUEST_RETURNING
    with db.transaction() as cur:
        if decision == "approve":
            pending = db.fetch_one(
                "SELECT scheduled_for FROM requests WHERE id = %s AND status = 'pending'",
                (request_id,))
            if pending is None:
                return None
            sched = pending.get("scheduled_for")
            deferred = sched is not None and sched > datetime.now(timezone.utc)
            new_status = "scheduled" if deferred else "approved"
            cur.execute(
                f"UPDATE requests SET status = %s, decided_by_slack_id = %s, "
                f" decided_by_name = %s, decided_at = NOW() "
                f"WHERE id = %s AND status = 'pending' RETURNING {ret}",
                (new_status, by_id, by_name, request_id))
            row = cur.fetchone()
            if row is None:
                return None
            audit.log_in(cur, request_id, by_id, by_name, "approved",
                         {"scheduled_for": str(sched) if sched else None,
                          "deferred": deferred})
            return Outcome(row=row, decision="approve", deferred=deferred)

        status = "rejected" if decision == "reject" else "changes_requested"
        cur.execute(
            f"UPDATE requests SET status = %s, decision_reason = %s, "
            f" decided_by_slack_id = %s, decided_by_name = %s, decided_at = NOW() "
            f"WHERE id = %s AND status = 'pending' RETURNING {ret}",
            (status, reason, by_id, by_name, request_id))
        row = cur.fetchone()
        if row is None:
            return None
        audit.log_in(cur, request_id, by_id, by_name,
                     "rejected" if decision == "reject" else "changes_requested",
                     {"reason": reason})
        return Outcome(row=row, decision=decision, deferred=False)


def apply_effects(client, outcome: Outcome) -> None:
    """Post-decision side effects, identical for Slack and web callers:
    update the admin approval message(s), notify the submitter, and — on an
    immediate approve — dispatch to the executor. Reads actor/reason off the
    decided row so it works regardless of which surface decided."""
    from . import executor, ratings
    from .slack_app import notifications

    row = outcome.row
    by = row.get("decided_by_slack_id")
    reason = row.get("decision_reason")
    is_bundle = bool(row.get("bundle_id"))

    if outcome.decision == "approve":
        if outcome.deferred:
            sched = row.get("scheduled_for")
            if is_bundle:
                notifications.update_bundle_admin_dms(client, row["bundle_id"])
            else:
                notifications.update_all_admin_messages(
                    client, row,
                    f":alarm_clock: Approved by <@{by}> — scheduled for "
                    f"`{sched:%Y-%m-%d %H:%M UTC}`",
                    keep_cancel_button=True)
                notifications.dm_user_scheduled(client, row)
            return
        if is_bundle:
            notifications.update_bundle_admin_dms(client, row["bundle_id"])
        else:
            notifications.update_all_admin_messages(
                client, row, f":white_check_mark: Approved by <@{by}>")
            notifications.dm_requester(
                client, row["requester_slack_id"],
                text=f"SQL request #{row['id']} approved — executing now.",
                blocks=notifications.requester_card_blocks(
                    row, status_emoji=":white_check_mark:",
                    status_text=f"Approved by <@{by}> — executing now"),
                color=notifications._status_color(
                    {"status": "approved"}, notifications._tier_color(row)))
            notifications.update_requester_card(
                client, row, status_emoji=":white_check_mark:",
                status_text=f"Approved by <@{by}>")
        executor.submit(row, client)
        return

    if is_bundle:
        notifications.update_bundle_admin_dms(client, row["bundle_id"])
        return

    if outcome.decision == "reject":
        notifications.update_all_admin_messages(
            client, row, f":x: Rejected by <@{by}> — {reason}")
        notifications.dm_requester(
            client, row["requester_slack_id"],
            text=f"SQL request #{row['id']} rejected.",
            blocks=notifications.requester_card_blocks(
                row, status_emoji=":x:", status_text=f"Rejected by <@{by}>",
                body_extra=f"*Reason:* {reason}")
            + [notifications.resubmit_action_block(row["id"])],
            color=notifications._status_color(
                {"status": "rejected"}, notifications._tier_color(row)))
        notifications.update_requester_card(
            client, row, status_emoji=":x:", status_text=f"Rejected by <@{by}>")
        ratings.maybe_prompt(client, row)
    else:  # changes
        notifications.update_all_admin_messages(
            client, row, f":pencil2: Changes requested by <@{by}> — {reason}")
        notifications.dm_requester(
            client, row["requester_slack_id"],
            text=f"SQL request #{row['id']} needs changes.",
            blocks=notifications.requester_card_blocks(
                row, status_emoji=":pencil2:",
                status_text=f"Changes requested by <@{by}>",
                body_extra=(f"*What to change:* {reason}\n\n"
                            f"Use *Edit & resubmit* below to reopen this query."))
            + [notifications.resubmit_action_block(row["id"])],
            color=notifications._status_color(
                {"status": "changes_requested"}, notifications._tier_color(row)))
        notifications.update_requester_card(
            client, row, status_emoji=":pencil2:",
            status_text=f"Changes requested by <@{by}>")
