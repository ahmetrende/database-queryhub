"""Slack UI for the access-request flow.

When `/sql` is rejected (no team grant for any target), the user is shown
an ephemeral message with a [Request access] button. Clicking it opens a
modal where they describe what target / database / query they want and why.
On submit, all active admins get a DM with the request and Approve / Reject
buttons. Approve writes the grant, so the DM carries no SQL to run.

This module owns the block-kit for that flow and the words of the decision.
Persistence is in `access_requests.py`. The Bolt registrations live in
`handlers.py`. The Slack buttons and the QueryHub Web screen both decide a
request, and both use the decision helpers here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

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
                    ":lock: You do not have access to any database target yet.\n"
                    "To ask for access, click *Request access*. An admin reviews "
                    "each request."
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
                        "Tell the admins which target and database you need. "
                        "Tell them what you want to run. An admin reviews the "
                        "request and grants access if it is appropriate."
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
                "label": {"type": "plain_text", "text": "Database (leave empty to use the default)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": A_DATABASE,
                    "placeholder": {"type": "plain_text", "text": "Example: payment_db"},
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
                "text": ":page_facing_up: _The thread below has the attempted SQL as a snippet._",
            }],
        })

    # The card carries no SQL. Approve writes the grant itself
    # (`access_requests.decide`). A recipe to paste first also went stale once:
    # after the pod cutover it named tables that were empty, and the paste
    # failed.
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                ":wrench: *Approve* gives the requester a per-user grant. "
                "The grant uses the requested tier (default RO) and the "
                "listed database."
            ),
        },
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
                            "Approve creates the per-user grant for this "
                            "request automatically, at the requested tier, for "
                            "the listed database. Then it notifies the user."
                        ),
                    },
                    "confirm": {"type": "plain_text", "text": "Yes, approve"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
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


# ---------- what a decision says, and who hears it ----------

def _until(expires_at) -> str:
    """`2026-10-14 11:09 UTC` for a datetime or an ISO string, `""` for none."""
    if not expires_at:
        return ""
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def approval_texts(updated: dict, decided_by: str) -> tuple[str, str]:
    """(status line for the admin cards, DM for the requester) after an
    approval.

    `updated` is the row `access_requests.decide` returned, with its
    `auto_grant` summary. `decided_by` is how the decider is shown: a Slack
    mention, or a plain name for an account that has no Slack id."""
    ag = updated.get("auto_grant") or {}
    if ag.get("applied"):
        dbs = ag.get("databases")
        db_txt = ", ".join(f"`{d}`" for d in dbs) if dbs else "_all databases_"
        until = _until(ag.get("expires_at"))
        grant_line = (f"\n:key: Granted automatically: *{(ag.get('mode') or 'ro').upper()}* "
                      f"on {db_txt}" + (f", until `{until}`" if until else "") + ".")
        requester_note = ("\n\nYou can now run `/sql` — your access is active."
                          + (f" It lasts until `{until}`." if until else ""))
    elif ag.get("reason") == "tier_conflict":
        grant_line = ("\n:warning: Auto-grant skipped: an active grant at a different "
                      f"tier (*{(ag.get('mode') or '?').upper()}*) already exists. "
                      "To give the requested tier, change that grant manually.")
        requester_note = "\n\nA DBA will finalize your access shortly."
    elif ag.get("reason") == "control_plane":
        grant_line = ("\n:no_entry: Auto-grant refused: this is the bot's own "
                      "control-plane database. It holds the audit log and the "
                      "grant tables. An access request cannot grant it.")
        requester_note = ("\n\nAn access request cannot grant this "
                          "connection.")
    elif ag.get("reason") == "no_target":
        grant_line = ("\n:warning: Auto-grant skipped: this server is not a "
                      "target yet. Onboard it, then grant access manually.")
        requester_note = "\n\nA DBA will finalize your access shortly."
    else:
        grant_line = ""
        requester_note = "\n\nYou can now run `/sql`."
    status_line = f":white_check_mark: Approved by {decided_by}" + grant_line
    requester_dm = (
        f":white_check_mark: *Access request `#{updated['id']}` approved* by "
        f"{decided_by}.\n" + access_context_md(updated) + requester_note
    )
    return status_line, requester_dm


def rejection_texts(updated: dict, decided_by: str, reason: str) -> tuple[str, str]:
    """(status line for the admin cards, DM for the requester) after a
    rejection."""
    status_line = f":x: Rejected by {decided_by} — {reason}"
    requester_dm = (
        f":x: *Access request `#{updated['id']}` rejected* by {decided_by}\n"
        + access_context_md(updated) + f"\n*Reason:* {reason}"
    )
    return status_line, requester_dm


def update_admin_cards(client, access_request_id: int, target,
                       status_line: str) -> None:
    """Replace the buttons on every admin's copy of the card with
    `status_line`, so nobody presses Approve on a request that is decided."""
    from .. import access_requests
    from . import notifications

    req = access_requests.get(access_request_id)
    if req is None:
        return
    blocks = resolved_admin_dm_blocks(req, target, status_line)
    for r in access_requests.list_admin_dms(access_request_id):
        try:
            notifications._update(
                client,
                channel=r["channel_id"],
                ts=r["message_ts"],
                blocks=blocks,
                text=status_line,
            )
        except Exception:
            log.exception(
                "Failed to update admin DM for access request %s (channel=%s ts=%s)",
                access_request_id, r["channel_id"], r["message_ts"],
            )


def announce_decision(client, updated: dict, target, status_line: str,
                      requester_dm: str) -> None:
    """After a decision: retire every admin's card, then tell the requester."""
    from . import notifications

    update_admin_cards(client, updated["id"], target, status_line)
    notifications.dm_requester(client, updated["requester_slack_id"], requester_dm)


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
