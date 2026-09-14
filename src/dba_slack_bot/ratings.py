"""Per-request user rating + optional feedback.

Sent as a follow-up DM after a request reaches a terminal state
(completed / failed / rejected / cancelled). Five buttons (1-5) + Skip;
a low rating (1-2) reveals a contextual "What went wrong?" feedback
button that opens a modal. One rating per request (E1: first lock wins).

Cooldown: a user who has rated anything in the last `COOLDOWN_DAYS` days
sees no new prompt — keeps survey fatigue bounded.

Feature flag: `bot_config.rating_enabled` (default 'on').
"""
from __future__ import annotations

import logging
import threading

from . import config as cfg
from . import db

# Delay before posting the rating prompt so the result message (CSV
# upload, completion DM) lands first in the user's DM thread. Slack's
# files_upload_v2 returns once the file is accepted but the DM
# rendering takes a moment; chat_postMessage is much faster, so without
# this delay the rating prompt visibly precedes the result.
PROMPT_DELAY_SECONDS = 2.0

log = logging.getLogger(__name__)

COOLDOWN_DAYS = 30

# Slack action_id constants — registered in handlers.register().
ACTION_RATE_PREFIX = "rating_"          # rating_1, rating_2, ..., rating_5
ACTION_SKIP = "rating_skip"
ACTION_ADD_FEEDBACK = "rating_add_feedback"
FEEDBACK_MODAL_CALLBACK = "rating_feedback_modal"
FEEDBACK_BLOCK_ID = "rating_feedback_block"
FEEDBACK_INPUT_ID = "rating_feedback_input"


def is_enabled() -> bool:
    return (cfg.get_setting("rating_enabled", "on")
            or "off").strip().lower() in {"on", "true", "yes", "1"}


def in_cooldown(principal_id: str) -> bool:
    """True if this user has rated anything in the last COOLDOWN_DAYS."""
    row = db.fetch_one(
        "SELECT 1 FROM request_ratings "
        "WHERE slack_user_id = %s "
        f"  AND rated_at >= NOW() - INTERVAL '{COOLDOWN_DAYS} days' "
        "LIMIT 1",
        (principal_id,),
    )
    return row is not None


def has_rating(request_id: int) -> bool:
    row = db.fetch_one(
        "SELECT 1 FROM request_ratings WHERE request_id = %s",
        (request_id,),
    )
    return row is not None


def save(request_id: int, principal_id: str, rating: int) -> bool:
    """Insert a rating. Returns True if inserted, False if a rating
    already exists for this request (E1 lock — first wins)."""
    if not (1 <= rating <= 5):
        raise ValueError(f"rating must be 1..5, got {rating}")
    row = db.fetch_one(
        "INSERT INTO request_ratings (request_id, slack_user_id, rating) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (request_id) DO NOTHING "
        "RETURNING id",
        (request_id, principal_id, rating),
    )
    return row is not None


def add_feedback(request_id: int, feedback: str) -> None:
    db.execute(
        "UPDATE request_ratings SET feedback_text = %s WHERE request_id = %s",
        (feedback, request_id),
    )


def get(request_id: int) -> dict | None:
    return db.fetch_one(
        "SELECT id, request_id, slack_user_id, rating, feedback_text, rated_at "
        "FROM request_ratings WHERE request_id = %s",
        (request_id,),
    )


# ---------- Slack block builders ----------

def prompt_blocks(request_id: int) -> list[dict]:
    """Initial prompt: 1-5 + Skip. Buttons read 'worst' on 1 and 'best'
    on 5 so users don't have to guess the polarity."""
    button_labels = {
        1: "1 — worst",
        2: "2",
        3: "3",
        4: "4",
        5: "5 — best",
    }
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":star: *How was this query?* Quick rating helps us "
                    f"improve. _(request `#{request_id}`)_"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": "rating_buttons",
            "elements": [
                *[
                    {
                        "type": "button",
                        "action_id": f"{ACTION_RATE_PREFIX}{n}",
                        "text": {"type": "plain_text", "text": button_labels[n]},
                        "value": str(request_id),
                    }
                    for n in range(1, 6)
                ],
                {
                    "type": "button",
                    "action_id": ACTION_SKIP,
                    "text": {"type": "plain_text", "text": "Skip"},
                    "value": str(request_id),
                },
            ],
        },
    ]


def thanks_blocks(request_id: int, rating: int, has_feedback: bool) -> list[dict]:
    """Replaces the prompt after a rating click. Low ratings (1-2) get
    a 'What went wrong?' button; high ratings get a milder offer."""
    star_count = "★" * rating + "☆" * (5 - rating)
    if has_feedback:
        return [{
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f":white_check_mark: Thanks! Rated *{rating}/5* "
                             f"({star_count}). Comment saved."},
        }]

    if rating <= 2:
        prompt = "Thanks! Sorry it wasn't great. Want to tell us what went wrong?"
        button_label = "What went wrong?"
    else:
        prompt = "Thanks for the rating!"
        button_label = "Add feedback"

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f":white_check_mark: *{rating}/5* ({star_count}) — {prompt}"},
        },
        {
            "type": "actions",
            "block_id": "rating_followup",
            "elements": [{
                "type": "button",
                "action_id": ACTION_ADD_FEEDBACK,
                "text": {"type": "plain_text", "text": button_label},
                "value": str(request_id),
            }],
        },
    ]


def skipped_blocks() -> list[dict]:
    return [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": "_Rating skipped._"},
    }]


def feedback_modal(request_id: int, rating: int) -> dict:
    label = "What went wrong?" if rating <= 2 else "Comments / suggestions"
    return {
        "type": "modal",
        "callback_id": FEEDBACK_MODAL_CALLBACK,
        "private_metadata": str(request_id),
        "title": {"type": "plain_text", "text": "Send feedback"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": FEEDBACK_BLOCK_ID,
                "label": {"type": "plain_text", "text": label},
                "element": {
                    "type": "plain_text_input",
                    "action_id": FEEDBACK_INPUT_ID,
                    "multiline": True,
                    "max_length": 2000,
                },
            }
        ],
    }


# ---------- public entry point ----------

def maybe_prompt(client, request: dict) -> None:
    """If enabled + user not in cooldown + no existing rating, schedule
    the rating prompt as a separate DM. The post is deferred via a
    daemon timer so the result message (CSV upload / completion DM)
    lands first. Best-effort: any error is logged and swallowed in the
    deferred worker so a Slack hiccup doesn't surface in the user DM."""
    try:
        if not cfg.ENV.slack_enabled or not is_enabled():
            return
        user_id = request.get("requester_slack_id")
        if not user_id:
            return
        request_id = request["id"]
        if has_rating(request_id):
            return
        if in_cooldown(user_id):
            return
    except Exception:  # noqa: BLE001
        log.exception("rating prompt pre-check failed for request %s",
                      request.get("id"))
        return

    threading.Timer(
        PROMPT_DELAY_SECONDS,
        _post_prompt,
        args=(client, user_id, request_id),
    ).start()


def _post_prompt(client, user_id: str, request_id: int) -> None:
    """Delayed worker: actually post the rating DM. Imports
    notifications lazily to keep ratings.py decoupled."""
    try:
        from .slack_app import notifications
        opened = client.conversations_open(users=user_id)
        channel = opened["channel"]["id"]
        notifications._post(
            client,
            channel=channel,
            text="Rate this query",
            blocks=prompt_blocks(request_id),
            **notifications.display_overrides(),
        )
    except Exception:  # noqa: BLE001
        log.exception("rating prompt failed for request %s", request_id)
