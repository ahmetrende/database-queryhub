"""Slack UI for /sql grant + /sql revoke — the admin access-granting tools.

Grant modal: pick a Slack user, a target (RDS), tier, optional database
restriction + reason. Revoke modal: pick a user, see their active grants,
revoke any with a button. Authorization + DB writes live in `grants.py`;
Bolt registrations in `handlers.py`. Imports no other slack_app module.
"""
from __future__ import annotations

import json

TIER_LABELS = {"ro": "RO — read only",
               "rw": "RW — read + write",
               "ddl": "DDL — schema changes"}

# ---- grant modal ----
GRANT_CALLBACK = "admin_grant_modal"
B_USER = "blk_grant_user"
A_USER = "act_grant_user"
B_TARGET = "blk_grant_target"
A_TARGET = "act_grant_target"
B_TIER = "blk_grant_tier"
A_TIER = "act_grant_tier"
B_DBS = "blk_grant_dbs"
A_DBS = "act_grant_dbs"
B_REASON = "blk_grant_reason"
A_REASON = "act_grant_reason"

# ---- revoke modal ----
REVOKE_CALLBACK = "admin_revoke_modal"
B_REVOKE_USER = "blk_revoke_user"
A_REVOKE_USER = "act_revoke_user"       # users_select, dispatch_action
ACTION_REVOKE_ONE = "act_revoke_one"    # per-grant button


def grant_modal(
    *,
    allowed_tiers: list[str],
    grantee: str | None = None,
    target_initial_options: list[dict] | None = None,
    tier: str | None = None,
    reason: str | None = None,
    target_ids: list[int] | None = None,
) -> dict:
    """Build the grant modal. Tier options are limited to what the granting
    admin may hand out (grants.allowed_tiers).

    The target multi-select uses dispatch_action: when it changes, a handler
    stores the picked target ids in private_metadata and re-renders the view.
    The database picker's options handler reads those ids from
    private_metadata — Slack does NOT reliably include another input's
    selection in a block_suggestion payload's state (the cascading-select
    quirk), so the live state can't be trusted for it. The other args let the
    re-render preserve what the user already entered."""
    tier_options = [
        {"text": {"type": "plain_text", "text": TIER_LABELS[t]}, "value": t}
        for t in allowed_tiers
    ]
    tier_initial = next(
        (o for o in tier_options if o["value"] == tier), tier_options[0])

    user_element: dict = {"type": "users_select", "action_id": A_USER,
                          "placeholder": {"type": "plain_text",
                                          "text": "Pick a Slack user"}}
    if grantee:
        user_element["initial_user"] = grantee

    target_element: dict = {
        "type": "multi_external_select", "action_id": A_TARGET,
        "min_query_length": 0,
        "placeholder": {"type": "plain_text",
                        "text": "Type to search; pick one or more"}}
    if target_initial_options:
        target_element["initial_options"] = target_initial_options

    reason_element: dict = {"type": "plain_text_input", "action_id": A_REASON,
                            "placeholder": {"type": "plain_text",
                                            "text": "e.g. onboarding to the auth team"}}
    if reason:
        reason_element["initial_value"] = reason

    return {
        "type": "modal",
        "callback_id": GRANT_CALLBACK,
        "title": {"type": "plain_text", "text": "Grant access"},
        "submit": {"type": "plain_text", "text": "Grant access"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps({"targets": target_ids or []}),
        "blocks": [
            {
                "type": "input", "block_id": B_USER,
                "label": {"type": "plain_text", "text": "Grant to"},
                "element": user_element,
            },
            {
                # Multi-select: grant the same tier + DB restriction on
                # several RDS at once. dispatch_action so a change updates
                # private_metadata (read by the DB picker) via views_update.
                "type": "input", "block_id": B_TARGET, "dispatch_action": True,
                "label": {"type": "plain_text", "text": "Targets (RDS) — one or more"},
                "element": target_element,
            },
            {
                "type": "input", "block_id": B_TIER,
                "label": {"type": "plain_text", "text": "Tier"},
                "element": {"type": "static_select", "action_id": A_TIER,
                            "options": tier_options,
                            "initial_option": tier_initial},
            },
            {
                # Multi-select fed by the A_DBS options handler, which reads
                # the picked target ids from private_metadata (see above) and
                # returns the union of their snapshotted databases.
                # Empty = all databases.
                "type": "input", "block_id": B_DBS, "optional": True,
                "label": {"type": "plain_text",
                          "text": "Databases (leave empty = all databases)"},
                "element": {"type": "multi_external_select", "action_id": A_DBS,
                            "min_query_length": 0,
                            "placeholder": {"type": "plain_text",
                                            "text": "Pick target(s) first, then choose databases"}},
            },
            {
                "type": "input", "block_id": B_REASON, "optional": True,
                "label": {"type": "plain_text", "text": "Reason (optional)"},
                "element": reason_element,
            },
        ],
    }


def _grant_line(g: dict) -> str:
    dbs = g.get("allowed_databases")
    scope = ", ".join(dbs) if dbs else "all databases"
    return f"*{g.get('alias') or g['target_server_id']}*  ·  {g['mode'].upper()}  ·  {scope}"


def revoke_user_option(row: dict) -> dict:
    """external_select option for a user who holds active grants."""
    name = row.get("name") or row["slack_user_id"]
    n = row["n_grants"]
    label = f"{name} — {n} grant" + ("" if n == 1 else "s")
    return {"text": {"type": "plain_text", "text": label[:75]},
            "value": row["slack_user_id"]}


def revoke_modal(*, selected_user: str | None = None,
                 selected_label: str | None = None,
                 grants: list[dict] | None = None) -> dict:
    """Revoke modal: a user picker (dispatch_action) plus, once a user is
    chosen, that user's active grants each with a Revoke button. Rebuilt
    via views_update on pick / after a revoke.

    The picker is an external_select fed by the A_REVOKE_USER options
    handler — it lists ONLY users who currently hold a grant (the
    revocable set), never the whole Slack workspace."""
    picker: dict = {"type": "external_select", "action_id": A_REVOKE_USER,
                    "min_query_length": 0,
                    "placeholder": {"type": "plain_text",
                                    "text": "Pick a user with grants"}}
    if selected_user:
        picker["initial_option"] = {
            "text": {"type": "plain_text",
                     "text": (selected_label or selected_user)[:75]},
            "value": selected_user}
    # An actions block (not input) so the pick fires immediately and the
    # modal needs no submit button — revoking happens via the per-grant
    # buttons below, not a form submit.
    blocks: list[dict] = [
        {"type": "actions", "block_id": B_REVOKE_USER, "elements": [picker]},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Only users with active grants are listed. Pick one to "
                    "see their grants, then Revoke any of them."}]},
    ]
    if selected_user and not grants:
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": f"<@{selected_user}> has no active per-user grants."}})
    for g in grants or []:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _grant_line(g)},
            "accessory": {
                "type": "button", "style": "danger",
                "action_id": ACTION_REVOKE_ONE,
                "text": {"type": "plain_text", "text": "Revoke"},
                "value": f"{selected_user}:{g['target_server_id']}",
                "confirm": {
                    "title": {"type": "plain_text", "text": "Revoke access?"},
                    "text": {"type": "mrkdwn",
                             "text": f"Remove <@{selected_user}>'s "
                                     f"{g['mode'].upper()} access to "
                                     f"`{g.get('alias')}`?"},
                    "confirm": {"type": "plain_text", "text": "Revoke"},
                    "deny": {"type": "plain_text", "text": "Keep"},
                },
            },
        })
    return {
        "type": "modal",
        "callback_id": REVOKE_CALLBACK,
        "title": {"type": "plain_text", "text": "Revoke access"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }
