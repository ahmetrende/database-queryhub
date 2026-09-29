"""The card a super-admin gets when an IDP sync is held (`idp_sync_guard`).

Blocks and wording only. The decision itself is `idp_sync_guard.decide`, and the
Bolt registrations are in `handlers.py`.
"""
from __future__ import annotations

ACTION_APPROVE = "act_idp_sync_approve"   # button on the card
ACTION_REJECT = "act_idp_sync_reject"     # button on the card

# People named on the card. A list of forty mentions is not a card anyone reads.
_SHOWN = 25


def _people(ids: list[str]) -> str:
    shown = ", ".join(f"<@{u}>" for u in ids[:_SHOWN])
    more = len(ids) - _SHOWN
    return shown + (f" and {more} more" if more > 0 else "")


def fallback(hold: dict) -> str:
    """The notification text, for clients that do not render blocks."""
    n = len(hold["would_disable"])
    return f"The IDP sync wants to disable {n} people. Do you approve?"


def blocks(hold: dict) -> list[dict]:
    """The approve / reject card. Button values carry the hold id."""
    n = len(hold["would_disable"])
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":warning: *The IDP sync wants to disable {n} people*\n"
                    f"The panel's list leaves out {n} people who can use QueryHub "
                    f"today. That is more than the limit of {hold['limit_at_hold']}, "
                    "so nothing has changed yet.\n*Do you approve?*"
                ),
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": _people(hold["would_disable"])}},
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": ("Approve lets the panel's next sync disable these people. "
                         "Reject keeps this list blocked until it changes."),
            }],
        },
        {
            "type": "actions",
            "block_id": f"idp_sync_actions_{hold['id']}",
            "elements": [
                {
                    "type": "button", "style": "primary",
                    "action_id": ACTION_APPROVE,
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": str(hold["id"]),
                },
                {
                    "type": "button", "style": "danger",
                    "action_id": ACTION_REJECT,
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": str(hold["id"]),
                },
            ],
        },
    ]


def decided_text(hold: dict, *, approved: bool, actor_id: str) -> str:
    """What replaces the buttons once someone has decided."""
    n = len(hold["would_disable"])
    if approved:
        return (f":white_check_mark: *Approved* by <@{actor_id}>. The panel's next "
                f"sync will disable these {n} people.")
    return (f":no_entry: *Rejected* by <@{actor_id}>. This list of {n} people stays "
            "blocked until it changes.")


def already_text(status: str) -> str:
    return f":information_source: This request is already *{status}*."
