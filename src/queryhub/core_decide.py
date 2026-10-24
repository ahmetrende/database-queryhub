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
    decide_bundle(bundle_id, decision, by_id, by_name, reason, may_decide)
        the same move for every open item of one batch, in ONE transaction:
        all items or none. Raises a BundleRefused subclass and writes nothing
        when it cannot decide them all.
    apply_effects(client, outcome)
        Slack mirror (admin messages + requester card) + executor.submit
        on approve. Safe to call from either process.
    require_kill_switch_off()
        The ONE check for every action that approves and runs a request.

The kill switch stops approvals too (operator decision, 2026-10-24). While it
is on, an approve is refused. Deny and request-changes stay open. `decide` and
`decide_bundle` make the check themselves for an approve, so a caller cannot
skip it. A path that approves without them (the Slack "Approve all remaining"
button, the CSV import approval) calls `require_kill_switch_off` directly.
tests/test_kill_switch_approve_guard.py fails when a new path skips it.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import audit, auto_approve, core_submit, db, idp_outbox

_VALID = ("approve", "reject", "changes")

KILL_SWITCH_APPROVE_MESSAGE = "Kill switch is on. Resume queries first."
KILL_SWITCH_APPROVE_CODE = "kill_switch_on"


class KillSwitchOn(Exception):
    """An approval that the kill switch refuses. Nothing was written.

    `code` is the web error code. `message` is the text for the person."""

    code = KILL_SWITCH_APPROVE_CODE

    def __init__(self, message: str = KILL_SWITCH_APPROVE_MESSAGE):
        super().__init__(message)
        self.message = message


def require_kill_switch_off() -> None:
    """Raise `KillSwitchOn` while the kill switch is on. Else do nothing.

    Every action that approves and runs a request calls this before it writes
    anything. A deny or a request for changes never calls it."""
    if core_submit.kill_switch_on():
        raise KillSwitchOn()


@dataclass
class Outcome:
    row: dict
    decision: str          # approve | reject | changes
    deferred: bool         # approve of a future-scheduled request → 'scheduled'


def _check(decision: str, reason: str | None) -> None:
    if decision not in _VALID:
        raise ValueError(f"decision must be one of {_VALID}, got {decision!r}")
    if decision in ("reject", "changes") and not (reason or "").strip():
        raise ValueError(f"{decision} requires a reason")


def decide(request_id: int, decision: str, *, by_id: str,
           by_name: str | None, reason: str | None = None) -> Outcome | None:
    """Atomically move a pending request to its decided state + write the
    audit row. Returns None if the request wasn't pending (already decided
    / raced). `reason` is required for reject/changes. An approve raises
    `KillSwitchOn` while the kill switch is on."""
    _check(decision, reason)
    if decision == "approve":
        require_kill_switch_off()
    with db.transaction() as cur:
        sched = None
        if decision == "approve":
            pending = db.fetch_one(
                "SELECT scheduled_for FROM requests WHERE id = %s AND status = 'pending'",
                (request_id,))
            if pending is None:
                return None
            sched = pending.get("scheduled_for")
        out = _write(cur, request_id, decision, by_id=by_id, by_name=by_name,
                     reason=reason, sched=sched)
    # After the commit: a lost outbox row must never undo the decision.
    if out is not None:
        idp_outbox.write_request_decided(out.row, out.decision)
    return out


def _write(cur, request_id: int, decision: str, *, by_id: str,
           by_name: str | None, reason: str | None, sched,
           extra: dict | None = None) -> Outcome | None:
    """The guarded UPDATE and its audit row, on the caller's cursor.

    `sched` is the pending row's `scheduled_for` (approve only). `extra` adds
    keys to the audit details. None when the row was not pending any more."""
    ret = core_submit.REQUEST_RETURNING
    if decision == "approve":
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
                      "deferred": deferred, **(extra or {})})
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
                 {"reason": reason, **(extra or {})})
    return Outcome(row=row, decision=decision, deferred=False)


# ---- one decision for a whole batch ------------------------------------------

class BundleRefused(Exception):
    """The batch decision wrote nothing. `item_ids` names the items that
    caused the refusal, where there are any."""

    def __init__(self, message: str, item_ids=()):
        super().__init__(message)
        self.item_ids = [int(i) for i in item_ids]


class BundleNotFound(BundleRefused):
    """No request carries this bundle id."""


class BundleOutOfScope(BundleRefused):
    """The caller may not decide one of the items."""


class BundleConflict(BundleRefused):
    """An item already has a decision, or no item waits for one."""


# What the lock reads: the state, and every column `admins.can_approve` reads.
_BUNDLE_COLS = ("id, status, scheduled_for, requester_slack_id, decided_by_slack_id, "
                "decided_by_name, query, target_server_id, engine, required_tier")


def _settled_at_submit(row: dict) -> bool:
    """True when the item never waited in the queue: a waiver (`AUTO`)
    approved it at submit, or a super-admin's access did in a row written
    before 2026-10-22. The decision skips such an item, and it is no conflict."""
    by = row.get("decided_by_slack_id")
    return by == auto_approve.AUTO_DECIDED_BY or auto_approve.is_super_decision(
        row.get("requester_slack_id"), by, row.get("decided_by_name"))


def decide_bundle(bundle_id: int, decision: str, *, by_id: str,
                  by_name: str | None, reason: str | None = None,
                  may_decide: Callable[[dict], bool]) -> list[Outcome]:
    """Decide every open item of one batch in ONE transaction: all or none.

    It locks the items first (`FOR UPDATE`), so a decision from Slack or a
    second admin cannot land between the check and the writes. Then, with
    nothing written yet:

      * no item at all                        -> BundleNotFound
      * `may_decide(row)` is False for an item
        that was not approved at submit       -> BundleOutOfScope
      * an item already decided by a person,
        or withdrawn, or expired              -> BundleConflict
      * no item waits for a decision          -> BundleConflict

    An item approved at submit (`_settled_at_submit`) never waited for a
    person, so the decision skips it. Each decided item gets its own audit
    row on the same cursor, with the bundle id in the details. A raised
    BundleRefused rolls the transaction back, so it writes nothing. The
    caller runs `apply_effects` for each outcome after the commit. An approve
    raises `KillSwitchOn` while the kill switch is on, and writes nothing."""
    _check(decision, reason)
    if decision == "approve":
        require_kill_switch_off()
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_BUNDLE_COLS} FROM requests WHERE bundle_id = %s "
            f"ORDER BY id FOR UPDATE",
            (bundle_id,))
        items = cur.fetchall()
        if not items:
            raise BundleNotFound(f"no batch {bundle_id}")
        considered = [r for r in items if not _settled_at_submit(r)]
        refused = [r["id"] for r in considered if not may_decide(r)]
        if refused:
            raise BundleOutOfScope(f"batch {bundle_id} has items outside the scope",
                                   refused)
        taken = [r["id"] for r in considered if r["status"] != "pending"]
        if taken:
            raise BundleConflict(f"batch {bundle_id} has items with a decision", taken)
        open_items = [r for r in considered if r["status"] == "pending"]
        if not open_items:
            raise BundleConflict(f"batch {bundle_id} has no item to decide")
        outcomes = []
        for r in open_items:
            out = _write(cur, r["id"], decision, by_id=by_id, by_name=by_name,
                         reason=reason, sched=r.get("scheduled_for"),
                         extra={"bulk": True, "bundle_id": bundle_id})
            if out is None:
                # Cannot happen under the row lock. Fail closed if it does.
                raise BundleConflict(f"batch {bundle_id} changed under the lock",
                                     [r["id"]])
            outcomes.append(out)
    # After the commit: one row per decided item.
    for out in outcomes:
        idp_outbox.write_request_decided(out.row, out.decision)
    return outcomes


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
