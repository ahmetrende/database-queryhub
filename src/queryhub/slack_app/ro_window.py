"""Slack UI for the RO-burst nudge + 1-hour auto-approve window request.

- `nudge_blocks()` renders the top-of-modal banner shown to a user who has
  made several RO requests in a short window (built by modal._auto_approve_banner).
- `request_modal()` is the justification modal opened by the banner button.
- `admin_dm_blocks()` is the Approve/Reject DM fanned out to admins.

Persistence is in `auto_approve_requests.py`; Bolt registrations in
`handlers.py`. This module imports no other slack_app module to avoid a
cycle with `modal.py` (which imports it).
"""
from __future__ import annotations

import json

# Action / callback IDs (distinct from the /sql modal + access flow)
ACTION_OPEN = "act_open_ro_window"          # banner button → opens request modal
MODAL_CALLBACK = "ro_window_request_modal"   # the justification modal
ACTION_APPROVE = "act_ro_window_approve"     # admin DM button
ACTION_REJECT = "act_ro_window_reject"       # admin DM button

# Block / element IDs inside the request modal
B_REASON = "blk_ro_window_reason"
A_REASON = "act_ro_window_reason"
B_TARGET = "blk_ro_window_target"
A_TARGET = "act_ro_window_target"
B_WINDOW = "blk_ro_window_window"
A_WINDOW = "act_ro_window_window"

# Selectable window durations (minutes, label). Default is the first that
# matches bot_config.ro_window_minutes (falls back to 60 = 1 hour).
WINDOW_OPTIONS: list[tuple[int, str]] = [
    (60, "1 hour"),
    (180, "3 hours"),
    (480, "8 hours"),
]
_VALID_WINDOW_MINUTES = frozenset(m for m, _ in WINDOW_OPTIONS)


def is_valid_window(minutes: int) -> bool:
    return minutes in _VALID_WINDOW_MINUTES


def _scope_label(target_alias: str, database_name: str | None) -> str:
    return f"`{target_alias}`" + (f" / `{database_name}`" if database_name else " (all dbs)")


def nudge_blocks(
    *,
    count: int,
    window_min: int,
    window_minutes: int,
    target_alias: str,
    target_server_id: int,
    database_name: str | None,
    has_active_grant: bool,
) -> list[dict]:
    """Banner blocks for a read-heavy user, rendered at the TOP of the modal.
    When they already hold an active auto-approve grant we only nudge toward
    Batch; otherwise we lead with a prominent window-request call to action.
    Uses :package: (not the help tip's :bulb:) so it doesn't blend in."""
    if has_active_grant:
        return [{
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f":package: You've run *{count}* reads in {window_min} min — "
                    "switch to *Batch* mode (below) to submit several in one "
                    "approval round."
                ),
            }],
        }]

    hrs = window_minutes / 60
    win_label = f"{int(window_minutes)} min" if window_minutes < 60 else (
        f"{int(hrs)}h" if hrs == int(hrs) else f"{window_minutes} min")
    return [
        {
            "type": "header",
            "text": {"type": "plain_text",
                     "text": ":zap: Running a lot of reads?", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"You've run *{count}* read queries in the last {window_min} "
                    f"min. Skip the per-query approval wait — get a *{win_label} "
                    f"read-only auto-approve* window for "
                    f"{_scope_label(target_alias, database_name)}. RO queries "
                    "there then dispatch immediately until it expires."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [{
                "type": "button",
                "action_id": ACTION_OPEN,
                "style": "primary",
                # Short label so it never truncates on narrow screens; the
                # section above already describes the window (length is picked
                # in the modal).
                "text": {"type": "plain_text", "text": "Request window"},
                "value": json.dumps({
                    "t": target_server_id,
                    "d": database_name,
                }),
            }],
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (":package: Or switch to *Batch* mode (below) to submit "
                         "several reads in one approval round."),
            }],
        },
    ]


def request_cta_blocks() -> list[dict]:
    """Modest, ALWAYS-available entry point for requesting an RO
    auto-approve window (shown when the user has no active grant and no
    read-burst). Opens the request modal cold — the user picks the target
    and window there."""
    # Section + accessory button: the description reflows to the screen
    # width while the button label stays short. A long plain button label
    # (e.g. "Request RO auto-approve window") gets truncated on narrow /
    # mobile screens — Block Kit buttons have no width control, so keeping
    # the label short is the only reliable fix.
    return [{
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": ":zap: *Read-only auto-approve* — skip per-query "
                         "approval on a target for a set window."},
        "accessory": {
            "type": "button",
            "action_id": ACTION_OPEN,
            "text": {"type": "plain_text", "text": "Request"},
            "value": "{}",
        },
    }]


def request_modal(
    *,
    user_targets: list,
    default_minutes: int = 60,
    preselect_target_id: int | None = None,
) -> dict:
    """Modal to request a scoped RO auto-approve window. The user PICKS the
    target and the window length (default 1h; 1/3/8h) and gives a reason.
    `user_targets` are the caller's allowed targets (TargetServer objects);
    `preselect_target_id` pre-selects one (e.g. the target from a read
    burst). Selections are read from the submitted state — nothing
    security-relevant is trusted from private_metadata; the handler
    re-checks the picked target against the user's grants."""
    tgt_options = [
        {"text": {"type": "plain_text", "text": t.alias[:75]}, "value": str(t.id)}
        for t in user_targets
    ]
    tgt_initial = next(
        (o for o in tgt_options if o["value"] == str(preselect_target_id)), None)

    default_minutes = default_minutes if is_valid_window(default_minutes) else 60
    win_options = [
        {"text": {"type": "plain_text", "text": label}, "value": str(mins)}
        for mins, label in WINDOW_OPTIONS
    ]
    win_initial = next(o for o in win_options if o["value"] == str(default_minutes))

    target_element = {
        "type": "static_select",
        "action_id": A_TARGET,
        "placeholder": {"type": "plain_text", "text": "Pick a target"},
        "options": tgt_options,
    }
    if tgt_initial:
        target_element["initial_option"] = tgt_initial

    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK,
        "title": {"type": "plain_text", "text": "Request RO window"},
        "submit": {"type": "plain_text", "text": "Request"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": "{}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":zap: *Read-only auto-approve window.* While active, your "
                        "SELECT queries on the chosen target dispatch immediately "
                        "(writes still need approval). An admin approves this "
                        "request first."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": B_TARGET,
                "label": {"type": "plain_text", "text": "Target"},
                "element": target_element,
            },
            {
                "type": "input",
                "block_id": B_WINDOW,
                "label": {"type": "plain_text", "text": "Window length"},
                "element": {
                    "type": "static_select",
                    "action_id": A_WINDOW,
                    "options": win_options,
                    "initial_option": win_initial,
                },
            },
            {
                "type": "input",
                "block_id": B_REASON,
                "label": {"type": "plain_text", "text": "Why do you need this? (required)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": A_REASON,
                    "multiline": True,
                    "min_length": 5,
                    "placeholder": {"type": "plain_text",
                                    "text": "e.g. investigating ticket PASS-123; many lookups on this DB"},
                },
            },
        ],
    }


def admin_dm_blocks(req: dict, target_alias: str) -> list[dict]:
    """Approve/Reject DM for admins. Button values carry the request id."""
    win = req["window_minutes"]
    win_label = f"{int(win/60)}h" if win % 60 == 0 else f"{win} min"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":zap: *Auto-approve window request* #{req['id']}\n"
                    f"<@{req['requester_slack_id']}> wants *{req['max_tier'].upper()}* "
                    f"auto-approve for *{win_label}* on "
                    f"{_scope_label(target_alias, req.get('database_name'))}."
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Reason:*\n>{req['reason']}"},
        },
        {
            "type": "actions",
            "block_id": f"ro_window_actions_{req['id']}",
            "elements": [
                {
                    "type": "button", "style": "primary",
                    "action_id": ACTION_APPROVE,
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": str(req["id"]),
                },
                {
                    "type": "button", "style": "danger",
                    "action_id": ACTION_REJECT,
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": str(req["id"]),
                },
            ],
        },
    ]
