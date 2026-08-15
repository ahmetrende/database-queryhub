"""Slack UI for the access-request flow.

When `/sql` is rejected (no team grant for any target), the user is shown
an ephemeral message with a [Request access] button. Clicking it opens a
modal where they describe what target / database / query they want and why.
On submit, all active admins get a DM with a copy-pasteable SQL snippet for
granting access, plus Approve / Reject buttons.

This module owns the block-kit for that flow; persistence is in
`access_requests.py`; the Bolt registrations live in `handlers.py`.
"""
from __future__ import annotations

import logging

from .. import config as cfg
from .. import targets

log = logging.getLogger(__name__)

# View callback IDs
MODAL_CALLBACK = "access_request_modal"
REJECT_MODAL_CALLBACK = "access_reject_modal"

# Action IDs
ACTION_OPEN_REQUEST = "act_open_access_request"
ACTION_APPROVE = "act_access_approve"
ACTION_REJECT = "act_access_reject"

# Block / element IDs (must differ from the /sql modal's so Bolt can route)
B_TARGET = "blk_access_target"
B_DATABASE = "blk_access_database"
B_QUERY = "blk_access_query"
B_REASON = "blk_access_reason"

A_TARGET = "act_access_target"
A_DATABASE = "act_access_database"
A_QUERY = "act_access_query"
A_REASON = "act_access_reason"


# ---------- ephemeral shown when /sql is blocked by team auth ----------

def blocked_ephemeral_blocks() -> list[dict]:
    """Block-kit body for the ephemeral the bot sends when a user with no
    team grants invokes /sql. Includes a [Request access] button that opens
    the access-request modal."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":lock: You don't have access to any database targets yet.\n"
                    "Use the button below to request access — admins will review."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_OPEN_REQUEST,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Request access"},
                    "value": "open",
                }
            ],
        },
    ]


# ---------- the request modal itself ----------

def build_request_modal() -> dict:
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK,
        "title": {"type": "plain_text", "text": "Request DB access"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Tell admins which target / database you need and what "
                        "you want to run. They'll review and grant if "
                        "appropriate."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": B_TARGET,
                "label": {"type": "plain_text", "text": "Target server"},
                "element": {
                    "type": "external_select",
                    "action_id": A_TARGET,
                    "min_query_length": 0,
                    "placeholder": {"type": "plain_text", "text": "Type to search..."},
                },
            },
            {
                "type": "input",
                "block_id": B_DATABASE,
                "optional": True,
                "label": {"type": "plain_text", "text": "Database (leave blank for default)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": A_DATABASE,
                    "placeholder": {"type": "plain_text", "text": "e.g. payment_db"},
                },
            },
            {
                "type": "input",
                "block_id": B_QUERY,
                "optional": True,
                "label": {"type": "plain_text", "text": "Query you want to run (optional)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": A_QUERY,
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "SELECT ..."},
                },
            },
            {
                "type": "input",
                "block_id": B_REASON,
                "label": {"type": "plain_text", "text": "Reason / use case"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": A_REASON,
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Why do you need access? Be specific.",
                    },
                },
            },
        ],
    }


def access_context_md(req: dict) -> str:
    """One-line server+database summary for an access-request DM. Falls back
    gracefully when the target was deleted between request and decision."""
    target = targets.get(req["target_server_id"]) if req.get("target_server_id") else None
    target_alias = target.alias if target else "?"
    db = req.get("database_name") or "_(default)_"
    return f"*Target:* `{target_alias}`  •  *Database:* `{db}`"


def options_for_targets(query: str = "") -> list[dict]:
    """ALL enabled targets — the access-request modal must show every server
    so the user can pick the one they don't yet have access to. No team
    filtering here (unlike /sql modal)."""
    matches = targets.search(query) if query else targets.list_enabled()
    return [
        {
            "text": {"type": "plain_text", "text": t.alias[:75]},
            "description": {
                "type": "plain_text",
                "text": f"{t.host}:{t.port}/{t.default_database}"[:75],
            },
            "value": str(t.id),
        }
        for t in matches[:100]
    ]


def parse_modal_submission(view_state: dict) -> dict:
    values = view_state["values"]
    target_id = int(values[B_TARGET][A_TARGET]["selected_option"]["value"])
    database = (values[B_DATABASE][A_DATABASE].get("value") or "").strip() or None
    attempted = (values[B_QUERY][A_QUERY].get("value") or "").strip() or None
    reason = (values[B_REASON][A_REASON].get("value") or "").strip()
    return {
        "target_server_id": target_id,
        "database_name": database,
        "attempted_query": attempted,
        "reason": reason,
    }


# ---------- admin DM blocks ----------

def admin_dm_blocks(access_request: dict, target: targets.TargetServer | None,
                    requested_server: str | None = None) -> list[dict]:
    """Block-kit body for the DM each admin gets when a new access request is
    submitted. The attempted SQL (if any) is NOT inlined here; it is uploaded
    as a thread snippet (see `handlers.handle_access_request_submission`).

    `requested_server` is the free-text server name from a web endpoint
    request for a target that isn't onboarded yet (target is None); shown
    so the admin sees WHAT was asked for instead of a bare "target removed"."""
    req_id = access_request["id"]
    requester_id = access_request["requester_slack_id"]
    if target:
        target_label = f"`{target.alias}` (id={target.id})\n_{target.host}_"
    elif requested_server:
        target_label = f"`{requested_server}`\n_(not onboarded — free-text request)_"
    else:
        target_label = "_(target removed)_"
    target_id_for_snippet = target.id if target else "<TARGET_ID>"
    db_label = (
        f"`{access_request['database_name']}`" if access_request["database_name"]
        else "_(default)_"
    )

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":bell: Access request"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Requester*\n<@{requester_id}>"},
                {"type": "mrkdwn", "text": f"*Target*\n{target_label}"},
                {"type": "mrkdwn", "text": f"*Database*\n{db_label}"},
                {"type": "mrkdwn", "text": f"*Request ID*\n`#{req_id}`"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reason*\n{access_request['reason']}",
            },
        },
    ]
    attempted = access_request.get("attempted_query") or ""
    if 0 < len(attempted) <= 500:
        # Short — show inline.
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Attempted query*\n```\n{attempted}\n```"},
        })
    elif attempted:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": ":page_facing_up: _The attempted SQL is attached as a snippet in the thread below._",
            }],
        })

    # Copy-paste snippet — admin runs this in DataGrip/psql before clicking
    # Approve. We only generate the *additive* snippet (existing-team path)
    # because creating a NEW team is a strategic decision; admin is pointed
    # at deploy/team_admin_templates.sql for that.
    db_arg = (
        f"ARRAY['{access_request['database_name']}']"
        if access_request.get("database_name") else "NULL"
    )
    snippet = (
        "-- Add user to existing team (replace TEAM_NAME):\n"
        f"INSERT INTO team_members (team_id, slack_user_id) VALUES\n"
        f"    ((SELECT id FROM teams WHERE name = 'TEAM_NAME'), '{requester_id}')\n"
        f"ON CONFLICT DO NOTHING;\n"
        "-- Ensure that team has the grant for this target+db:\n"
        f"INSERT INTO team_target_grants (team_id, target_server_id, allowed_databases) VALUES\n"
        f"    ((SELECT id FROM teams WHERE name = 'TEAM_NAME'), {target_id_for_snippet}, {db_arg})\n"
        f"ON CONFLICT DO NOTHING;"
    )
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                ":wrench: *Approve below auto-grants* the requester per-user "
                "access at the requested tier (default RO) for the listed "
                "database. Prefer a *team-level* grant instead? Run the SQL "
                "below first — the auto-grant then only adds a narrower or "
                "equal per-user row. For a *new* team see "
                "`deploy/team_admin_templates.sql`."
            ),
        },
    })
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"```{snippet}```"},
    })

    blocks.append({
        "type": "actions",
        "block_id": f"access_req_{req_id}",
        "elements": [
            {
                "type": "button",
                "action_id": ACTION_APPROVE,
                "style": "primary",
                "text": {"type": "plain_text", "text": "Approve"},
                "value": str(req_id),
                "confirm": {
                    "title": {"type": "plain_text", "text": "Approve and grant?"},
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "Approving creates the per-user grant for this "
                            "request automatically (requested tier, listed "
                            "database) and notifies the user."
                        ),
                    },
                    "confirm": {"type": "plain_text", "text": "Yes, approve"},
                    "deny": {"type": "plain_text", "text": "Wait"},
                },
            },
            {
                "type": "button",
                "action_id": ACTION_REJECT,
                "style": "danger",
                "text": {"type": "plain_text", "text": "Reject"},
                "value": str(req_id),
            },
        ],
    })
    return blocks


def resolved_admin_dm_blocks(
    access_request: dict,
    target: targets.TargetServer | None,
    status_line: str,
) -> list[dict]:
    """Same as admin_dm_blocks but with the action buttons replaced by a
    plain status line — used by chat.update after a decision is made."""
    blocks = admin_dm_blocks(access_request, target)[:-1]  # drop actions
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": status_line}],
    })
    return blocks


# ---------- reject reason modal ----------

def build_reject_modal(access_request_id: int) -> dict:
    return {
        "type": "modal",
        "callback_id": REJECT_MODAL_CALLBACK,
        "private_metadata": str(access_request_id),
        "title": {"type": "plain_text", "text": "Reject access request"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "reason_block",
                "label": {"type": "plain_text", "text": "Reason"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reason_input",
                    "multiline": True,
                },
            }
        ],
    }


def fan_out_admin_dms(client, new_row: dict, target,
                      requested_server: str | None = None) -> str | None:
    """DM every active admin about a new access request, recording each
    DM for lockstep updates on decision. Shared by the Slack blocked-
    query flow and the web "Request new endpoint" endpoint. Returns the
    first delivered message ts (None if nothing delivered — e.g. no active
    admins, or no Slack transport configured).

    Callers must read None as "not notified", never as "not saved"."""
    from .. import access_requests, admins
    from . import notifications

    # Vanilla (no-Slack) profile: there is no client to send with, so stop
    # before touching it. Every structural sibling already opens this way
    # (notify_admins, notify_admins_import, notify_admins_bundle); this one
    # did not. `client` is None in the vanilla profile, so conversations_open
    # below raised AttributeError, the per-admin `except Exception` swallowed
    # it once per admin, and the function returned None — which the web caller
    # turned into a 503 for a request that had in fact been saved.
    if not cfg.ENV.slack_enabled or client is None:
        return None

    blocks = admin_dm_blocks(new_row, target, requested_server=requested_server)
    alias = target.alias if target else "an unlisted server"
    fallback = (
        f"Access request from <@{new_row['requester_slack_id']}> for {alias}"
        f" — request #{new_row['id']}"
    )
    overrides = notifications.display_overrides()
    first_ts: str | None = None
    for adm in admins.list_active():
        admin_id = adm["slack_user_id"]
        try:
            opened = client.conversations_open(users=admin_id)
            channel_id = opened["channel"]["id"]
            posted = notifications._post(client,
                channel=channel_id, blocks=blocks, text=fallback, **overrides,
            )
            first_ts = first_ts or posted["ts"]
            access_requests.record_admin_dm(
                new_row["id"], admin_id, channel_id, posted["ts"],
            )
            attempted = (new_row.get("attempted_query") or "").strip()
            if attempted and len(attempted) > notifications.INLINE_QUERY_MAX_CHARS:
                notifications._upload_query_snippet(
                    client, channel_id, new_row["id"], attempted, posted["ts"],
                )
        except Exception:
            log.exception("Failed to DM admin %s about access request %s",
                          admin_id, new_row["id"])
    return first_ts
