"""Slack Bolt handlers: slash command, modal submit, button actions."""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MAX_UPLOAD_BYTES = 256 * 1024  # 256KB cap on uploaded SQL files


def _reserve_quietly(user_id: str) -> int | None:
    """Reserve a request id, or None. Never raises.

    Opening a query screen matters more than the number on it, so a database
    hiccup here means a modal without an id — not a `/sql` that does not open.
    core_submit._claim_draft treats a missing id as "use a fresh one".
    """
    try:
        return core_submit.reserve_request_id(user_id)
    except Exception:
        log.warning("could not reserve a request id for %s", user_id, exc_info=True)
        return None


def _with_req_id(view: dict, req_id: int | None) -> dict:
    """Put the reserved request id into the view's private_metadata.

    Every later rebuild merges rather than replaces (modal.merge_pm), so the id
    set here survives target switches, template loads and cascades — otherwise
    each rebuild would drop it and the number on screen would change.
    """
    if req_id:
        view["private_metadata"] = modal.merge_pm(
            view.get("private_metadata"), req_id=req_id)
    return view


# ---- Kill-switch: bot_config-driven master toggle to halt new traffic ----

def _kill_switch_on() -> bool:
    return core_submit.kill_switch_on()


def _kill_switch_message() -> str:
    return core_submit.kill_switch_message()

from slack_bolt import Ack, App
from slack_sdk.web import WebClient

from .. import access_requests, admins, audit, auto_approve, auto_approve_requests, bundles, csv_import, db, executor, favorites, grants, pre_flight, profile_sync, query_safety, ratings, requesters, schema_catalog, targets, teams, templates
from .. import config as cfg
from .. import core_submit
from .. import core_decide
from . import access, admin_grant, modal, notifications, ro_window, schema_browser, subcommands

log = logging.getLogger(__name__)


def register(app: App) -> None:
    app.command("/sql")(handle_slash_sql)
    app.options(modal.A_SERVER)(handle_server_options)
    app.options(modal.A_LOAD_TEMPLATE)(handle_load_template_options)
    app.action(modal.A_LOAD_TEMPLATE)(handle_load_template)
    app.options(modal.A_LOAD_HISTORY)(handle_load_history_options)
    app.action(modal.A_LOAD_HISTORY)(handle_load_history)
    app.options(modal.A_LOAD_FAVORITE)(handle_load_favorite_options)
    app.action(modal.A_LOAD_FAVORITE)(handle_load_favorite)
    # Database action_id is dynamic (act_database_v<target_id>) so the Slack
    # client treats the dropdown as fresh after target changes. Register the
    # options handler with a regex that matches both forms.
    app.options(re.compile(rf"^{re.escape(modal.A_DATABASE)}"))(handle_database_options)
    app.action(modal.A_SERVER)(handle_target_selected)  # cascade fix
    app.view(modal.MODAL_CALLBACK_ID)(handle_view_submission)

    # Batch modal handlers — per-item action_ids carry a 1-based suffix
    # (e.g. act_b_server_3); database action_ids additionally have a
    # _v<tid> salt for cache busting. Regex-register everything that
    # matches the prefix.
    app.action(modal.A_MODE_TOGGLE)(handle_mode_toggle)
    app.options(re.compile(rf"^{re.escape(modal.BATCH_A_SERVER)}_\d+$"))(handle_batch_server_options)
    app.options(re.compile(rf"^{re.escape(modal.BATCH_A_DATABASE)}_\d+"))(handle_batch_database_options)
    app.action(re.compile(rf"^{re.escape(modal.BATCH_A_SERVER)}_\d+$"))(handle_batch_target_selected)
    app.action(modal.BATCH_A_ADD_ITEM)(handle_batch_add_item)
    app.action(modal.BATCH_A_REMOVE_ITEM)(handle_batch_remove_item)
    app.view(modal.BATCH_MODAL_CALLBACK_ID)(handle_batch_submission)

    app.action(notifications.ACTION_APPROVE)(handle_approve)
    app.action(notifications.ACTION_REJECT)(handle_reject)
    app.action(notifications.ACTION_REQUEST_CHANGES)(handle_request_changes)
    app.action(notifications.ACTION_CANCEL_SCHEDULED)(handle_cancel_scheduled)
    app.action(notifications.ACTION_CANCEL_REQUEST)(handle_cancel_request)
    app.action(notifications.ACTION_DBA_MARK_COMPLETED)(handle_dba_mark_completed)
    app.action(notifications.ACTION_DBA_MARK_FAILED)(handle_dba_mark_failed)

    # Bulk bundle decision buttons + the matching reject-reason modal.
    app.action(notifications.ACTION_BUNDLE_APPROVE_ALL)(handle_bundle_approve_all)
    app.action(notifications.ACTION_BUNDLE_REJECT_ALL)(handle_bundle_reject_all)
    app.view("bundle_reject_modal")(handle_bundle_reject_submission)
    app.view("reject_modal")(handle_reject_submission)
    app.view("changes_modal")(handle_changes_submission)
    app.view("dba_failed_modal")(handle_dba_failed_submission)

    # Access-request flow.
    app.action(access.ACTION_OPEN_REQUEST)(handle_open_access_request)
    app.options(access.A_TARGET)(handle_access_target_options)
    app.view(access.MODAL_CALLBACK)(handle_access_request_submission)
    app.action(access.ACTION_APPROVE)(handle_access_approve)
    app.action(access.ACTION_REJECT)(handle_access_reject)
    app.view(access.REJECT_MODAL_CALLBACK)(handle_access_reject_submission)

    # RO-burst nudge → 1-hour RO auto-approve window request flow.
    app.action(ro_window.ACTION_OPEN)(handle_open_ro_window)
    app.view(ro_window.MODAL_CALLBACK)(handle_ro_window_submission)
    app.action(ro_window.ACTION_APPROVE)(handle_ro_window_approve)
    app.action(ro_window.ACTION_REJECT)(handle_ro_window_reject)

    # Schema browser (pushed on top of the /sql modal).
    app.action(schema_browser.ACTION_OPEN)(handle_open_schema_browser)
    app.options(schema_browser.A_TABLE)(handle_schema_table_options)
    app.action(schema_browser.A_TABLE)(handle_schema_table_selected)

    # Admin grant / revoke tools (/sql grant, /sql revoke).
    app.options(admin_grant.A_TARGET)(handle_grant_target_options)
    app.action(admin_grant.A_TARGET)(handle_grant_target_changed)
    app.options(admin_grant.A_DBS)(handle_grant_db_options)
    app.view(admin_grant.GRANT_CALLBACK)(handle_grant_submission)
    app.options(admin_grant.A_REVOKE_USER)(handle_revoke_user_options)
    app.action(admin_grant.A_REVOKE_USER)(handle_revoke_user_picked)
    app.action(admin_grant.ACTION_REVOKE_ONE)(handle_revoke_click)

    # Rating flow (post-completion DM buttons + optional feedback modal).
    app.action(re.compile(rf"^{re.escape(ratings.ACTION_RATE_PREFIX)}\d$"))(handle_rating_click)
    app.action(ratings.ACTION_SKIP)(handle_rating_skip)
    app.action(ratings.ACTION_ADD_FEEDBACK)(handle_rating_add_feedback)
    app.view(ratings.FEEDBACK_MODAL_CALLBACK)(handle_rating_feedback_submit)

    # CSV import
    app.options(modal.A_IMPORT_SERVER)(handle_import_server_options)
    app.view(modal.IMPORT_MODAL_CALLBACK_ID)(handle_import_submission)
    app.action(notifications.ACTION_IMPORT_APPROVE)(handle_import_approve)
    app.action(notifications.ACTION_IMPORT_REJECT)(handle_import_reject)
    app.action(notifications.ACTION_RESUBMIT)(handle_resubmit)
    app.action(notifications.ACTION_FAVORITE)(handle_favorite_button)


def handle_slash_sql(ack: Ack, body: dict, client: WebClient, respond) -> None:
    user_id = body.get("user_id", "")
    text = (body.get("text") or "").strip()

    # Sub-command path: /sql <subcmd ...>. Bypasses the modal. Allowlist
    # is still enforced (no roles/info leak to off-allowlist users), but
    # kill_switch + team-grant gate are skipped because sub-commands
    # don't submit a query.
    if text:
        ack()
        if not requesters.is_allowed(user_id) and not admins.is_admin(user_id):
            respond({
                "response_type": "ephemeral",
                "text": (
                    ":no_entry: *You don't have access to `/sql`.*\n"
                    "Reach out to the DBA team to be added."
                ),
            })
            return
        profile_sync.maybe_backfill_user_profile(client, user_id)
        subcommands.dispatch(text, user_id, client, respond, body)
        return

    # Layer 0: master kill switch. Drains all new submissions while still
    # letting admins approve/reject already-pending requests.
    if _kill_switch_on():
        ack()
        respond({"response_type": "ephemeral", "text": _kill_switch_message()})
        return

    # Layer 1: requester allowlist (kill-switch). Backed by the `requesters`
    # table; admins always pass; an empty table means open to all.
    if not requesters.is_allowed(user_id):
        ack()
        log.info("blocked /sql from non-allowlisted user %s", user_id)
        respond({
            "response_type": "ephemeral",
            "text": (
                ":no_entry: *You don't have access to `/sql`.*\n"
                "Reach out to the DBA team to be added."
            ),
        })
        return

    # Best-effort: if this user has a row in requesters/admins without an
    # email, grab it from Slack now. Silent on failure.
    profile_sync.maybe_backfill_user_profile(client, user_id)

    # Layer 2: team-based authorization. Admins always pass; non-admins must
    # belong to at least one team that has a target grant. Otherwise we offer
    # a [Request access] button that opens the access-request modal.
    if not teams.has_any_grant(user_id):
        ack()
        log.info("/sql from user %s with no team grants — offering request flow", user_id)
        respond({
            "response_type": "ephemeral",
            "text": "You don't have access to any database targets yet.",
            "blocks": access.blocked_ephemeral_blocks(),
        })
        return

    ack()
    # Reserve the id this request will carry, so the modal shows it before the
    # user has typed anything and the number never changes on them. Best-effort:
    # /sql opening is worth more than a number, so a failure here is a modal
    # without one, not a failed command.
    reserved_req_id = _reserve_quietly(user_id)

    client.views_open(
        trigger_id=body["trigger_id"],
        view=_with_req_id(modal.build_modal(principal_id=user_id,
                                          req_id=reserved_req_id),
                          reserved_req_id),
    )
    # Access log: the user opened the /sql submit modal. Best-effort — never let
    # an audit hiccup break the modal. Name is resolved from `requesters` in the
    # admin audit view, so actor_name can be left None here.
    try:
        audit.log(None, user_id, None, "slack_sql_opened", {"surface": "slack"})
    except Exception:
        log.warning("audit: slack_sql_opened failed for %s", user_id, exc_info=True)


def handle_server_options(ack: Ack, payload: dict, body: dict) -> None:
    query = (payload.get("value") or "").strip()
    user_id = body.get("user", {}).get("id") or body.get("user_id", "")
    ack({"options": modal.options_for_targets(user_id, query)})


def _target_id_from_view(view: dict) -> int | None:
    """Read the chosen target_server_id out of the modal's private_metadata,
    which `handle_target_selected` writes via views.update each time the
    target dropdown changes. We DON'T rely on view.state.values for this —
    Slack often omits an external_select's selected_option from state.values
    when a *different* element fires a block_suggestion (cascading dropdowns
    quirk)."""
    pm = (view or {}).get("private_metadata") or ""
    if not pm:
        return None
    try:
        data = json.loads(pm)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    tid = data.get("target_id")
    return int(tid) if tid is not None else None


def handle_load_template_options(ack: Ack, payload: dict, body: dict) -> None:
    """Typeahead for the modal's Template picker. Owner-first, then
    shared templates the user can see. Each option shows alias / db
    in parentheses so the user can tell two same-named templates apart
    when one is theirs and one is a shared one (rare but possible
    across owners)."""
    typed = (payload.get("value") or "").strip()
    user_id = body.get("user", {}).get("id") or body.get("user_id", "")
    rows = templates.search_for_picker(user_id, typed, limit=100)
    options = []
    for r in rows:
        target = r.get("target_alias") or "—"
        db_name = r.get("database_name") or "—"
        shared_tag = "" if r["is_owned"] else "  (shared)"
        label = f"{r['name']} — {target}/{db_name}{shared_tag}"
        options.append({
            "text": {"type": "plain_text", "text": label[:75]},
            "value": str(r["id"]),
        })
    ack({"options": options})


def handle_load_template(ack: Ack, body: dict, client: WebClient) -> None:
    """User picked a template in the modal's external_select. Re-render
    the modal with target / db / query pre-filled from the chosen
    template. Slack preserves the user's other field values (justification,
    schedule, format radio) because we keep their block_ids identical."""
    ack()
    sel = body.get("actions", [{}])[0].get("selected_option") or {}
    val = sel.get("value")
    if not val:
        return
    try:
        tpl_id = int(val)
    except (TypeError, ValueError):
        return
    tpl = templates.get(tpl_id)
    if tpl is None:
        return

    # Verify the picker still has the right to load this template
    # (paranoia: if it became un-shared between options and action,
    # we don't want a stale value to leak someone else's private query).
    user_id = body.get("user", {}).get("id")
    if not tpl["is_shared"] and tpl["owner_slack_id"] != user_id:
        log.info(
            "load_template: refusing tpl#%s — not visible to %s anymore",
            tpl_id, user_id,
        )
        return

    view = body.get("view") or {}
    view_id, view_hash = view.get("id"), view.get("hash")
    if not view_id:
        return

    fresh = modal.build_modal(target_id=tpl["target_server_id"],
                              principal_id=user_id)
    # Pin the chosen target_id in private_metadata. external_select
    # `initial_option` is purely visual — it never makes it into
    # view.state.values on submit unless the user actually clicks the
    # dropdown. Without this pm row, parse_submission would have no
    # target_id and the submit would 500. Same mechanism
    # handle_target_selected already uses for the cascade.
    # Same trick as target / database below: Slack's external_select
    # and plain_text_input both preserve the `initial_*` value
    # visually after a views.update, but ONLY put it back in
    # state.values on submit if the user touched the element. So we
    # pin every loaded field in private_metadata; parse_submission
    # falls back to these when state.values is empty.
    # merge_pm, not json.dumps: this rebuild must not drop the reserved
    # request id the modal was opened with.
    fresh["private_metadata"] = modal.merge_pm(
        body.get("view", {}).get("private_metadata"),
        target_id=tpl["target_server_id"],
        database=tpl["database_name"],
        query=tpl["query"],
    )
    # Re-use the batch→single injector: same shape (target / db /
    # query / wants_result / result_format). _inject_single_initials
    # walks the freshly-built view and stamps initial values on the
    # matching block_ids.
    _inject_single_initials(fresh, {
        "target_server_id": tpl["target_server_id"],
        "target_alias": None,    # resolved below
        "database_name": tpl["database_name"],
        "query": tpl["query"],
        # Templates only persist target/db/query; leave format + wants
        # at whatever the user already toggled in this open modal.
    }, prev_view=view)
    if tpl["target_server_id"]:
        t = targets.get(tpl["target_server_id"])
        if t:
            for blk in fresh["blocks"]:
                if blk.get("block_id") == modal.B_SERVER:
                    blk["element"]["initial_option"] = {
                        "text": {"type": "plain_text", "text": t.alias[:75]},
                        "value": str(tpl["target_server_id"]),
                    }
                    break
    # Keep the Template picker showing the choice the user just made.
    for blk in fresh["blocks"]:
        if blk.get("block_id") == modal.B_LOAD_TEMPLATE:
            blk["element"]["initial_option"] = sel
            break

    try:
        client.views_update(view_id=view_id, hash=view_hash, view=fresh)
        templates.record_use(tpl["id"])
    except Exception:
        log.exception("views.update on load_template failed (tpl=%s)", tpl_id)


# Slack caps both private_metadata and a plain_text_input's initial_value at
# 3000 chars. We pin the prefilled query into private_metadata (submit-time
# fallback) AND the textarea, so a query that can't fit either can't be
# loaded into the form. Gate on a single threshold below 3000 (leaving room
# for target_id + database in the private_metadata JSON) so the two never
# disagree — a query under it round-trips fully; one over it degrades to a
# clear "too long for the form" notice instead of a silently-failed update.
_MODAL_PREFILL_QUERY_MAX = 2800


def _normalize_query(q: str | None) -> str | None:
    """Strip trailing whitespace per line + leading/trailing blank lines.
    Queries pasted from some tools carry heavy right-padding (hundreds of
    trailing spaces per line) that inflates the char count well past Slack's
    modal limits without adding any content."""
    if not q:
        return q
    lines = [ln.rstrip() for ln in q.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _reopen_modal_prefilled(client: WebClient, body: dict, *, user_id: str,
                            target_id: int | None, database: str | None,
                            query: str | None, picker_block_id: str,
                            picker_sel: dict) -> bool:
    """Re-render the open /sql modal with target / db / query pre-filled
    (shared by the history + favorite pickers; same mechanism as
    handle_load_template). `picker_block_id`/`picker_sel` keep the picker
    showing the choice the user just made. Returns True on success."""
    view = body.get("view") or {}
    view_id, view_hash = view.get("id"), view.get("hash")
    if not view_id:
        return False
    query = _normalize_query(query)
    # A query longer than the modal can carry must NOT go into the textarea
    # or private_metadata (Slack rejects the whole views.update if either
    # exceeds 3000 chars). Prefill target/db only and tell the user to paste
    # or upload it instead — degrade gracefully rather than fail silently.
    too_long = bool(query) and len(query) > _MODAL_PREFILL_QUERY_MAX
    pinned_query = None if too_long else query
    fresh = modal.build_modal(target_id=target_id, principal_id=user_id)
    # external_select / plain_text_input `initial_*` are visual-only and
    # don't reach state.values on submit unless touched — pin them in
    # private_metadata so parse_submission can fall back to them.
    fresh["private_metadata"] = modal.merge_pm(
        body.get("view", {}).get("private_metadata"),
        target_id=target_id, database=database, query=pinned_query,
    )
    _inject_single_initials(fresh, {
        "target_server_id": target_id,
        "target_alias": None,
        "database_name": database,
        "query": pinned_query,
    }, prev_view=view)
    if too_long:
        notice = {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (f":warning: _That saved query is {len(query):,} "
                         "characters — too long to load into the form "
                         "(Slack caps it at 3000). Target and database are "
                         "set; paste the SQL above or upload it as a .sql "
                         "file._"),
            }],
        }
        for i, b in enumerate(fresh["blocks"]):
            if b.get("block_id") == modal.B_QUERY:
                fresh["blocks"].insert(i + 1, notice)
                break
    if target_id:
        t = targets.get(target_id)
        if t:
            for blk in fresh["blocks"]:
                if blk.get("block_id") == modal.B_SERVER:
                    blk["element"]["initial_option"] = {
                        "text": {"type": "plain_text", "text": t.alias[:75]},
                        "value": str(target_id),
                    }
                    break
    for blk in fresh["blocks"]:
        if blk.get("block_id") == picker_block_id:
            blk["element"]["initial_option"] = picker_sel
            break
    try:
        client.views_update(view_id=view_id, hash=view_hash, view=fresh)
        return True
    except Exception:
        log.exception("views.update on prefill failed (block=%s)", picker_block_id)
        return False


# --- recent history picker --------------------------------------------------

def handle_load_history_options(ack: Ack, payload: dict, body: dict) -> None:
    """Typeahead for the modal's 'recent history' picker: the user's 10
    most-recent DISTINCT queries, newest first. Options are labelled with
    a query preview + target/db so two similar runs are distinguishable."""
    typed = (payload.get("value") or "").strip().lower()
    user_id = body.get("user", {}).get("id") or body.get("user_id", "")
    rows = db.fetch_all(
        """
        WITH recent AS (
            SELECT DISTINCT ON (md5(r.query))
                   r.id, r.query, r.database_name, r.created_at,
                   (SELECT alias FROM target_servers WHERE id = r.target_server_id)
                       AS target_alias
              FROM requests r
             WHERE r.requester_slack_id = %s
               -- a draft holds a reserved id and an empty query: never offer
               -- it as something to reuse
               AND r.status <> 'draft'
             ORDER BY md5(r.query), r.created_at DESC
        )
        SELECT * FROM recent ORDER BY created_at DESC LIMIT 10
        """,
        (user_id,),
    )
    options = []
    for r in rows:
        prev = " ".join((r["query"] or "").split())[:55]
        target = r.get("target_alias") or "—"
        db_name = r.get("database_name") or "—"
        if typed and typed not in prev.lower() and typed not in target.lower():
            continue
        label = f"{prev}  ·  {target}/{db_name}"
        options.append({
            "text": {"type": "plain_text", "text": label[:75]},
            "value": str(r["id"]),
        })
    ack({"options": options})


def handle_load_history(ack: Ack, body: dict, client: WebClient) -> None:
    """User picked one of their recent requests — prefill the modal from it."""
    ack()
    sel = body.get("actions", [{}])[0].get("selected_option") or {}
    val = sel.get("value")
    user_id = body.get("user", {}).get("id")
    if not val:
        return
    try:
        rid = int(val)
    except (TypeError, ValueError):
        return
    req = db.fetch_one(
        "SELECT requester_slack_id, target_server_id, database_name, query "
        "FROM requests WHERE id = %s", (rid,))
    # Owner-only: never prefill from someone else's request.
    if req is None or req["requester_slack_id"] != user_id:
        return
    _reopen_modal_prefilled(
        client, body, user_id=user_id,
        target_id=req["target_server_id"], database=req["database_name"],
        query=req["query"], picker_block_id=modal.B_LOAD_HISTORY, picker_sel=sel,
    )


# --- favorites picker -------------------------------------------------------

def handle_load_favorite_options(ack: Ack, payload: dict, body: dict) -> None:
    """Typeahead for the modal's favorites picker — the user's starred
    queries, most-recently-used first."""
    typed = (payload.get("value") or "").strip()
    user_id = body.get("user", {}).get("id") or body.get("user_id", "")
    rows = favorites.search_for_picker(user_id, typed, limit=100)
    options = []
    for r in rows:
        label_txt = r.get("label") or favorites.preview(r["query"])
        target = r.get("target_alias") or "—"
        db_name = r.get("database_name") or "—"
        label = f"{label_txt}  ·  {target}/{db_name}"
        options.append({
            "text": {"type": "plain_text", "text": label[:75]},
            "value": str(r["id"]),
        })
    ack({"options": options})


def handle_load_favorite(ack: Ack, body: dict, client: WebClient) -> None:
    """User picked a favorite — prefill the modal from it."""
    ack()
    sel = body.get("actions", [{}])[0].get("selected_option") or {}
    val = sel.get("value")
    user_id = body.get("user", {}).get("id")
    if not val:
        return
    try:
        fav_id = int(val)
    except (TypeError, ValueError):
        return
    fav = favorites.get(fav_id)
    # Owner-only: favorites are personal.
    if fav is None or fav["slack_user_id"] != user_id:
        return
    ok = _reopen_modal_prefilled(
        client, body, user_id=user_id,
        target_id=fav["target_server_id"], database=fav["database_name"],
        query=fav["query"], picker_block_id=modal.B_LOAD_FAVORITE, picker_sel=sel,
    )
    if ok:
        favorites.record_use(fav_id)


def handle_favorite_button(ack: Ack, body: dict, client: WebClient) -> None:
    """⭐ button on a completed-request DM: star that request's query for
    the requester. Owner-only (the button carries the request id; we still
    confirm the clicker owns it)."""
    ack()
    uid = body.get("user", {}).get("id")
    try:
        rid = int(body["actions"][0]["value"])
    except (KeyError, IndexError, ValueError, TypeError):
        return
    req = db.fetch_one(
        "SELECT requester_slack_id, target_server_id, database_name, query "
        "FROM requests WHERE id = %s", (rid,))
    if req is None or req["requester_slack_id"] != uid:
        return
    favorites.add(
        principal_id=uid, query=req["query"],
        target_server_id=req["target_server_id"],
        database_name=req["database_name"],
    )
    # Confirm via an ephemeral-style DM line; the button stays (re-clicking
    # is a harmless no-op dedup).
    try:
        opened = client.conversations_open(users=uid)
        notifications._post(client,
            channel=opened["channel"]["id"],
            text=f":star: Saved query `#{rid}` to your favorites — "
                 f"pick it from the *Load from favorites* dropdown in `/sql`.",
        )
    except Exception:
        log.exception("favorite confirm DM failed (req=%s)", rid)


def handle_target_selected(ack: Ack, body: dict, client: WebClient) -> None:
    """Fired whenever the user picks a target in the /sql modal. We stash the
    target_id in the modal's private_metadata via views.update so the
    database typeahead handler can look up the user's selection — Slack
    doesn't reliably propagate it through view.state.values for cascading
    external_selects."""
    ack()
    try:
        target_id = int(body["actions"][0]["selected_option"]["value"])
    except (KeyError, ValueError, TypeError) as e:
        log.warning("handle_target_selected: couldn't parse target_id (%s)", e)
        return
    view = body.get("view") or {}
    view_id = view.get("id")
    view_hash = view.get("hash")
    if not view_id:
        log.warning("handle_target_selected: no view.id, skipping views.update")
        return
    # Rebuilding private_metadata drops the stale database/query pins on
    # purpose (the target just changed) — but an edit-in-progress marker
    # must survive, or an edited resubmission that switched targets would
    # silently leave the original request pending.
    pm_dict: dict = {"target_id": target_id}
    try:
        old_pm = json.loads(view.get("private_metadata") or "{}")
        if isinstance(old_pm, dict) and old_pm.get("supersedes"):
            pm_dict["supersedes"] = old_pm["supersedes"]
    except (ValueError, TypeError):
        pass
    pm = json.dumps(pm_dict)
    # Rebuild blocks from scratch via modal.build_modal — guarantees the
    # `dispatch_action: true` flag survives the round-trip. Pass target_id
    # so the database element's action_id changes (act_database_v<id>),
    # forcing Slack's client to invalidate its cached options for the
    # dropdown and re-fetch when the user opens it next. Slack preserves
    # user-typed input per (block_id, action_id), so the SQL textarea and
    # justification stay intact through the rebuild.
    fresh = modal.build_modal(
        target_id=target_id,
        principal_id=body.get("user", {}).get("id"),
        # pm_dict was merged from the existing private_metadata above, so this
        # keeps the reserved id on screen across a target switch. Without it the
        # rebuild would drop the context line and the number would vanish.
        req_id=pm_dict.get("req_id"),
    )
    new_view = {
        "type": "modal",
        "callback_id": fresh["callback_id"],
        "title": fresh["title"],
        "submit": fresh["submit"],
        "close": fresh["close"],
        "blocks": fresh["blocks"],
        "private_metadata": pm,
    }
    try:
        client.views_update(view_id=view_id, hash=view_hash, view=new_view)
        log.info("target_selected: target_id=%d → private_metadata updated", target_id)
    except Exception as e:
        log.exception("views.update on target_selected failed: %s", e)


def handle_database_options(ack: Ack, payload: dict, body: dict) -> None:
    """Database dropdown typeahead. Reads the user's pick of target server
    from the modal's private_metadata and queries inventory."""
    typed = (payload.get("value") or "").strip()
    user_id = body.get("user", {}).get("id") or body.get("user_id", "")
    target_id = _target_id_from_view(body.get("view") or {})
    options = modal.options_for_databases(user_id, target_id, typed)
    log.info(
        "db-options: user=%s target_id=%s typed=%r pm=%r → %d options",
        user_id, target_id, typed,
        (body.get("view") or {}).get("private_metadata") or "",
        len(options),
    )
    ack({"options": options})


# ===========================================================================
# Batch modal — interactive handlers
# ===========================================================================
#
# Pattern mirrors the single-shot modal but every per-item action_id carries
# a 1-based suffix (`act_b_server_3`), so we register the handlers via regex
# and pull the index from the action_id. State across views.update is read
# from view.state + private_metadata via modal.read_batch_state_from_view.


def _read_single_modal_state(view: dict) -> dict:
    """Pull the fields the single-shot modal cares about out of
    view.state — used when toggling single ↔ batch so we can carry the
    user's typed query into the new layout."""
    values = (view or {}).get("state", {}).get("values", {})
    # Target (external_select).
    server = values.get(modal.B_SERVER, {}).get(modal.A_SERVER, {})
    selected_target = server.get("selected_option") or {}
    target_id = None
    target_alias = None
    if selected_target.get("value"):
        try:
            target_id = int(selected_target["value"])
        except (TypeError, ValueError):
            target_id = None
        target_alias = ((selected_target.get("text") or {}).get("text") or "")
        target_alias = target_alias.replace("[disabled] ", "", 1) or None
    # Database — dynamic action_id, scan by prefix.
    db_section = values.get(modal.B_DATABASE, {})
    db_block = next(
        (v for k, v in db_section.items() if k.startswith(modal.A_DATABASE)),
        {},
    )
    db_selected = db_block.get("selected_option") or {}
    database_name = db_selected.get("value") or None
    # Query / result-format / justification / schedule.
    query = (values.get(modal.B_QUERY, {}).get(modal.A_QUERY, {}).get("value") or "").strip()
    wants, result_format = modal._read_result_format(
        values, modal.B_WANTS_RESULT, modal.A_WANTS_RESULT,
    )
    justification = (values.get(modal.B_JUSTIFICATION, {})
                           .get(modal.A_JUSTIFICATION, {})
                           .get("value") or "").strip()
    sched_date = (values.get(modal.B_SCHEDULE_DATE, {})
                        .get(modal.A_SCHEDULE_DATE, {})
                        .get("selected_date"))
    sched_time = (values.get(modal.B_SCHEDULE_TIME, {})
                        .get(modal.A_SCHEDULE_TIME, {})
                        .get("selected_time"))
    return {
        "target_server_id": target_id,
        "target_alias": target_alias,
        "database_name": database_name,
        "query": query,
        "wants_result": wants,
        "result_format": result_format,
        "justification": justification,
        "schedule_date": sched_date,
        "schedule_time": sched_time,
    }


def handle_mode_toggle(ack: Ack, body: dict, client: WebClient) -> None:
    """Radio at the top of the modal switched between Single and Batch.
    Carry as much state as we can across the switch:

      single → batch : single's target / db / query / wants_result
                       become batch item #1; single's justification +
                       schedule become bundle-level.
      batch → single : item #1's target / db / query / wants_result
                       become single's; bundle justification + schedule
                       carried; items #2+ silently dropped (the user
                       chose to narrow the submission, accept the loss).
    """
    ack()
    selected = body.get("actions", [{}])[0].get("selected_option") or {}
    target_mode = selected.get("value") or modal.MODE_SINGLE
    view = body.get("view") or {}
    view_id = view.get("id")
    view_hash = view.get("hash")
    if not view_id:
        return

    current_cb = view.get("callback_id")
    fresh: dict
    if target_mode == modal.MODE_BATCH and current_cb == modal.MODAL_CALLBACK_ID:
        # Switching FROM single TO batch.
        s = _read_single_modal_state(view)
        # Resolve target host:port lazily so the context line under
        # batch item #1 matches the single-modal context.
        host_port = None
        if s["target_server_id"]:
            t = targets.get(s["target_server_id"])
            if t is not None:
                host_port = f"{t.host}:{t.port}"
        items_state = [{
            "target_server_id": s["target_server_id"],
            "target_alias":     s["target_alias"],
            "target_host_port": host_port,
            "database_name":    s["database_name"],
            "query":            s["query"],
            "wants_result":     s["wants_result"],
            "result_format":    s["result_format"],
        }]
        fresh = modal.build_batch_modal(
            item_count=1,
            items_state=items_state,
            justification=s["justification"],
            schedule_date=s["schedule_date"],
            schedule_time=s["schedule_time"],
            max_items=bundles.max_items(),
            principal_id=body.get("user", {}).get("id"),
        )
    elif target_mode == modal.MODE_SINGLE and current_cb == modal.BATCH_MODAL_CALLBACK_ID:
        # Switching FROM batch TO single — keep item #1's fields.
        items = modal.read_batch_state_from_view(view)
        first = items[0] if items else {}
        # build_modal takes target_id (for db dropdown salt). The rest
        # of the fields can't be passed as kwargs in the current
        # single modal API — Slack preserves typed input across a
        # views.update IF the (block_id, action_id) pair matches, but
        # we're swapping callback_id so state DOESN'T carry. We rebuild
        # below by manually injecting initial_value into the standard
        # blocks.
        fresh = modal.build_modal(
            target_id=first.get("target_server_id"),
            principal_id=body.get("user", {}).get("id"),
        )
        _inject_single_initials(fresh, first, view)
    else:
        # Same mode toggled — no-op (Slack still fires the action).
        return

    try:
        client.views_update(view_id=view_id, hash=view_hash, view=fresh)
    except Exception as e:
        log.exception("mode_toggle views.update failed: %s", e)


def _inject_single_initials(view_dict: dict, first_item: dict, prev_view: dict) -> None:
    """Mutate a freshly-built single-shot modal so its target / db /
    query / wants_result blocks come pre-filled from the batch's
    item #1 + previous bundle justification / schedule."""
    # Server initial_option
    if first_item.get("target_server_id") and first_item.get("target_alias"):
        for blk in view_dict["blocks"]:
            if blk.get("block_id") == modal.B_SERVER:
                blk["element"]["initial_option"] = {
                    "text": {"type": "plain_text",
                             "text": first_item["target_alias"][:75]},
                    "value": str(first_item["target_server_id"]),
                }
    if first_item.get("database_name"):
        for blk in view_dict["blocks"]:
            if blk.get("block_id") == modal.B_DATABASE:
                blk["element"]["initial_option"] = {
                    "text": {"type": "plain_text",
                             "text": first_item["database_name"][:75]},
                    "value": first_item["database_name"],
                }
    if first_item.get("query"):
        for blk in view_dict["blocks"]:
            if blk.get("block_id") == modal.B_QUERY:
                blk["element"]["initial_value"] = first_item["query"]
    # Result-format radio: carry the explicit format if the previous
    # batch item had one; else mirror wants_result. "none" maps to the
    # No-file option; "csv"/"xlsx" map to themselves.
    desired = first_item.get("result_format")
    if desired not in ("csv", "xlsx", "none"):
        desired = "csv" if first_item.get("wants_result") else "none"
    for blk in view_dict["blocks"]:
        if blk.get("block_id") == modal.B_WANTS_RESULT:
            for opt in blk["element"]["options"]:
                if opt["value"] == desired:
                    blk["element"]["initial_option"] = opt
                    break

    # Bundle-level justification + schedule come from the prev view.
    prev_values = (prev_view or {}).get("state", {}).get("values", {})
    just = (prev_values.get(modal.BATCH_B_JUSTIFICATION, {})
                       .get(modal.BATCH_A_JUSTIFICATION, {})
                       .get("value") or "").strip()
    if just:
        for blk in view_dict["blocks"]:
            if blk.get("block_id") == modal.B_JUSTIFICATION:
                blk["element"]["initial_value"] = just
    sd = (prev_values.get(modal.BATCH_B_SCHEDULE_DATE, {})
                     .get(modal.BATCH_A_SCHEDULE_DATE, {})
                     .get("selected_date"))
    if sd:
        for blk in view_dict["blocks"]:
            if blk.get("block_id") == modal.B_SCHEDULE_DATE:
                blk["element"]["initial_date"] = sd
    st = (prev_values.get(modal.BATCH_B_SCHEDULE_TIME, {})
                     .get(modal.BATCH_A_SCHEDULE_TIME, {})
                     .get("selected_time"))
    if st:
        for blk in view_dict["blocks"]:
            if blk.get("block_id") == modal.B_SCHEDULE_TIME:
                blk["element"]["initial_time"] = st

    # Add a context line so the user knows items #2+ were dropped (if any).
    items = modal.read_batch_state_from_view(prev_view)
    if len(items) > 1:
        notice = {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (f":information_source: _Switched to single mode — "
                         f"only item #1 carried over. {len(items) - 1} "
                         f"other item(s) discarded._"),
            }],
        }
        # Insert right after the mode toggle (which is the second block
        # when batch is enabled). Find it by block_id.
        insert_at = 0
        for i, b in enumerate(view_dict["blocks"]):
            if b.get("block_id") == modal.B_MODE_TOGGLE:
                insert_at = i + 1
                break
        view_dict["blocks"].insert(insert_at, notice)


def _batch_item_index_from_action_id(action_id: str, prefix: str) -> int | None:
    """`act_b_server_3` → 3. Returns None on malformed input (the handler
    then just acks without doing anything — safe failure mode)."""
    if not action_id.startswith(prefix + "_"):
        return None
    rest = action_id[len(prefix) + 1:]
    # Drop the optional `_v<tid>` salt that database action_ids carry.
    head = rest.split("_v", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def handle_batch_target_selected(ack: Ack, body: dict, client: WebClient) -> None:
    """Fired on every target dropdown change inside the batch modal. We
    read the full per-item state, re-render the modal with the new target
    selection captured in private_metadata, and refresh the db dropdown's
    action_id salt so Slack invalidates its options cache for that item."""
    ack()
    action = body.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    idx = _batch_item_index_from_action_id(action_id, modal.BATCH_A_SERVER)
    if idx is None:
        log.warning("batch target_selected: bad action_id %r", action_id)
        return
    view = body.get("view") or {}
    view_id = view.get("id")
    view_hash = view.get("hash")
    if not view_id:
        return
    pm = modal.decode_pm(view.get("private_metadata"))
    items_state = modal.read_batch_state_from_view(view)
    fresh = modal.build_batch_modal(
        item_count=pm["n"],
        items_state=items_state,
        max_items=bundles.max_items(),
        principal_id=body.get("user", {}).get("id"),
    )
    try:
        client.views_update(view_id=view_id, hash=view_hash, view=fresh)
    except Exception as e:
        log.exception("batch views.update on target_selected failed: %s", e)


def handle_batch_server_options(ack: Ack, payload: dict, body: dict) -> None:
    """Same as handle_server_options but for per-item batch dropdowns —
    the user's allowed targets don't change between rows."""
    user_id = body.get("user", {}).get("id") or body.get("user_id", "")
    typed = (payload.get("value") or "").strip()
    ack({"options": modal.options_for_targets(user_id, typed)})


def handle_batch_database_options(ack: Ack, payload: dict, body: dict) -> None:
    """Per-item database typeahead. The item's selected target lives in
    private_metadata under the per-item index (decoded from the action_id)."""
    typed = (payload.get("value") or "").strip()
    user_id = body.get("user", {}).get("id") or body.get("user_id", "")
    action_id = payload.get("action_id", "")
    idx = _batch_item_index_from_action_id(action_id, modal.BATCH_A_DATABASE)
    if idx is None:
        ack({"options": []})
        return
    view = body.get("view") or {}
    pm = modal.decode_pm(view.get("private_metadata"))
    items = pm.get("items", [])
    target_id = None
    if 0 <= idx - 1 < len(items):
        target_id = items[idx - 1].get("tid")
    ack({"options": modal.options_for_databases(user_id, target_id, typed)})


def handle_batch_add_item(ack: Ack, body: dict, client: WebClient) -> None:
    """[+ Add another item] — re-renders the modal with item_count + 1.
    Preserves all already-entered state."""
    ack()
    view = body.get("view") or {}
    view_id = view.get("id")
    view_hash = view.get("hash")
    if not view_id:
        return
    pm = modal.decode_pm(view.get("private_metadata"))
    items_state = modal.read_batch_state_from_view(view)
    max_n = bundles.max_items()
    new_n = min(pm["n"] + 1, max_n)
    if new_n == pm["n"]:
        return  # already at cap
    fresh = modal.build_batch_modal(
        item_count=new_n,
        items_state=items_state,
        max_items=max_n,
        principal_id=body.get("user", {}).get("id"),
    )
    try:
        client.views_update(view_id=view_id, hash=view_hash, view=fresh)
    except Exception as e:
        log.exception("batch add_item views.update failed: %s", e)


def handle_batch_remove_item(ack: Ack, body: dict, client: WebClient) -> None:
    """[Remove item #N] — drops the N-th item, shifts subsequent items
    down, re-renders. Never drops below 1 item."""
    ack()
    action = body.get("actions", [{}])[0]
    try:
        drop_idx = int(action.get("value", "1"))
    except (ValueError, TypeError):
        return
    view = body.get("view") or {}
    view_id = view.get("id")
    view_hash = view.get("hash")
    if not view_id:
        return
    items_state = modal.read_batch_state_from_view(view)
    if len(items_state) <= 1:
        return
    if not (1 <= drop_idx <= len(items_state)):
        return
    items_state.pop(drop_idx - 1)
    fresh = modal.build_batch_modal(
        item_count=len(items_state),
        items_state=items_state,
        max_items=bundles.max_items(),
        principal_id=body.get("user", {}).get("id"),
    )
    try:
        client.views_update(view_id=view_id, hash=view_hash, view=fresh)
    except Exception as e:
        log.exception("batch remove_item views.update failed: %s", e)


def _download_uploaded_query(client: WebClient, file_id: str) -> str:
    """Fetch an uploaded .sql/.txt file's contents from Slack. Bails on
    anything over 256KB (keeps the modal honest — anyone running a 1MB
    SQL file via this UI is misusing the bot)."""
    info = client.files_info(file=file_id)
    file_meta = info.get("file") or {}
    size = int(file_meta.get("size") or 0)
    if size > _MAX_UPLOAD_BYTES:
        raise ValueError(
            f"file is {size // 1024} KB, max allowed is {_MAX_UPLOAD_BYTES // 1024} KB"
        )
    download_url = file_meta.get("url_private_download") or file_meta.get("url_private")
    if not download_url:
        raise RuntimeError("no download URL in files.info response")
    req = urllib.request.Request(
        download_url,
        headers={"Authorization": f"Bearer {cfg.ENV.slack_bot_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise ValueError(f"file exceeds {_MAX_UPLOAD_BYTES // 1024} KB cap")
    return raw.decode("utf-8", errors="replace")


def _log_submission_failure(body: dict, user: dict, mode: str, errors: dict) -> None:
    """Quietly record a rejected modal submission to submission_failures.
    Best-effort: a logging failure must never block the user's ack. No
    admin notification — the operator greps this table on demand."""
    if not errors:
        return
    query = target_id = database = None
    try:
        view = body.get("view") or {}
        if mode == "batch":
            parsed = modal.parse_batch_submission(view)
            items = parsed.get("items") or []
            if items:
                # First item as context; the errors map names the rest.
                query = items[0].get("query")
                target_id = items[0].get("target_server_id")
                database = items[0].get("database_name")
        else:
            parsed = modal.parse_submission(view)
            query = parsed.get("query")
            target_id = parsed.get("target_server_id")
            database = parsed.get("database_name")
    except Exception:
        pass  # context is a bonus; the error map is the point
    try:
        db.execute(
            "INSERT INTO submission_failures "
            "(slack_user_id, slack_user_name, mode, target_server_id, "
            " database_name, query, errors) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            (user.get("id"), user.get("name") or user.get("username"), mode,
             target_id, database, query, json.dumps(errors)),
        )
    except Exception:
        log.exception("failed to log submission failure for user %s",
                      user.get("id"))


def _ack_logging(ack: Ack, body: dict, mode: str) -> Ack:
    """Wrap `ack` so any validation_errors response is logged to
    submission_failures first. Lets the existing 27 ack(errors) call
    sites stay untouched — we intercept centrally at the ack boundary."""
    user = body.get("user", {})

    def wrapped(payload=None):
        if isinstance(payload, dict) and payload.get("response_action") == "errors":
            _log_submission_failure(body, user, mode, payload.get("errors") or {})
        return ack(payload) if payload is not None else ack()

    return wrapped


def _finish_supersede(client: WebClient, user_id: str, old_id: int,
                      superseded_row: dict | None, new_row: dict) -> None:
    """Post-transaction notifications for an edited resubmission. When the
    original was withdrawn: admin cards lose their buttons and the old
    requester card gets a terminal 'superseded' line. When the withdraw
    raced an admin decision: warn the requester that BOTH requests exist."""
    if superseded_row is not None:
        if _is_bundle_item(superseded_row):
            notifications.update_bundle_admin_dms(
                client, superseded_row["bundle_id"])
        else:
            notifications.update_all_admin_messages(
                client, superseded_row,
                f":pencil2: Superseded by request #{new_row['id']} — "
                f"withdrawn by the requester",
            )
            notifications.update_requester_card(
                client, superseded_row,
                status_emoji=":pencil2:",
                status_text=f"Superseded — edited into request #{new_row['id']}",
            )
        return
    notifications.dm_requester(
        client, user_id,
        f":warning: Request `#{old_id}` had already been decided while you "
        f"were editing, so it could not be withdrawn. Your edit was "
        f"submitted separately as request `#{new_row['id']}` — check both "
        f"so the same change doesn't run twice.",
    )


def handle_view_submission(ack: Ack, body: dict, client: WebClient) -> None:
    """Single-query modal submit. Parsing + Slack-specific side effects
    live here; every security-relevant step (kill switch, rate limit,
    safety, grants, pre-flight, schedule, duplicate guard, auto-approve
    resolution, INSERT + audit, admin fan-out, dispatch) is the shared
    core_submit pipeline — the web API runs the exact same code."""
    ack = _ack_logging(ack, body, "single")
    user = body["user"]

    parsed = modal.parse_submission(body["view"])

    # Resolve the SQL: file_input takes precedence; fall back to the text box.
    if parsed.get("query_file_id"):
        try:
            file_query = _download_uploaded_query(client, parsed["query_file_id"])
        except Exception as e:
            log.exception("failed to download uploaded SQL file")
            ack({
                "response_action": "errors",
                "errors": {modal.B_QUERY_FILE: f"Could not read the uploaded file: {e}"},
            })
            return
        if not file_query.strip():
            ack({
                "response_action": "errors",
                "errors": {modal.B_QUERY_FILE: "Uploaded file is empty."},
            })
            return
        parsed["query"] = file_query.strip()

    if not parsed["query"]:
        ack({
            "response_action": "errors",
            "errors": {modal.B_QUERY:
                       "Provide your SQL — paste it here, or upload a .sql file below."},
        })
        return

    prep = core_submit.validate_submission(
        user["id"],
        user.get("name") or user.get("username"),
        target_server_id=parsed["target_server_id"],
        database_name=parsed["database_name"],
        query=parsed["query"],
        justification=parsed["justification"],
        wants_result=parsed["wants_result"],
        result_format=parsed.get("result_format", "csv"),
        schedule_date=parsed["schedule_date"],
        schedule_time=parsed["schedule_time"],
        origin="slack",
    )
    if isinstance(prep, core_submit.Rejection):
        ack({
            "response_action": "errors",
            "errors": {_FIELD_TO_BLOCK.get(prep.field, modal.B_QUERY): prep.message},
        })
        return

    ack()

    # Edited resubmission of a still-pending request (marker set by
    # handle_resubmit): the original is withdrawn in the SAME transaction
    # that creates its replacement, so admins can never approve the stale
    # version after the new one exists.
    supersedes_id: int | None = None
    try:
        _pm = json.loads((body.get("view") or {}).get("private_metadata") or "{}")
        if isinstance(_pm, dict) and _pm.get("supersedes"):
            supersedes_id = int(_pm["supersedes"])
    except (ValueError, TypeError):
        supersedes_id = None

    # Claim the id the modal has been showing since it opened. None — an older
    # modal, or a reservation that failed — simply takes a fresh id.
    outcome = core_submit.create_request(
        prep, supersedes_id=supersedes_id, draft_id=parsed.get("req_id"))
    if isinstance(outcome, core_submit.Rejection):
        # A concurrent submission raced the rate-limit / duplicate guard and
        # lost at INSERT time. The modal is already closed
        # (we ack'd above), so surface it by DM instead of a modal error.
        notifications.dm_requester(client, user["id"], f":warning: {outcome.message}")
        return
    row = outcome.row

    if supersedes_id is not None and supersedes_id != row["id"]:
        _finish_supersede(client, user["id"], supersedes_id,
                          outcome.superseded_row, row)

    # Optional: save / overwrite a named template alongside this run.
    # Lives in its own transaction — if it fails the request still
    # succeeded; we just log + ephemeral the operator.
    _maybe_save_template(client, user, parsed, prep.target.id, prep.database)
    _maybe_save_favorite(client, user, parsed, prep.target.id, prep.database)

    result = core_submit.dispatch_and_notify(client, prep, outcome,
                                             dm_requester=True)
    if result == "pending":
        # Reinforce the modal banner: DM the same RO-burst nudge once the
        # user crosses the threshold (the banner alone is easy to miss).
        _maybe_dm_ro_burst(client, user["id"], prep.required_mode)


# Rejection.field (transport-neutral) -> modal block id, so core
# validation failures render on the right modal input.
_FIELD_TO_BLOCK = {
    "kill_switch": modal.B_QUERY,
    "rate_limit": modal.B_QUERY,
    "query": modal.B_QUERY,
    "server": modal.B_SERVER,
    "database": modal.B_DATABASE,
    "justification": modal.B_JUSTIFICATION,
    "schedule_date": modal.B_SCHEDULE_DATE,
    "schedule_time": modal.B_SCHEDULE_TIME,
}


def _maybe_dm_ro_burst(client: WebClient, principal_id: str, required_mode: str) -> None:
    """If this submit just crossed the RO-burst threshold AND the user has no
    active auto-approve grant, DM them the same nudge the /sql modal banner
    shows. Fires once per crossing (count == threshold, not on every later
    request). Never raises — a nudge failure must not affect the submission."""
    if required_mode != "ro":
        return
    try:
        burst = modal._recent_ro_burst(principal_id)
        if not burst or burst["count"] != cfg.get_int("ro_burst_threshold", 3):
            return
        tier, _, _ = auto_approve.best_active_tier(principal_id)
        if tier is not None:
            return  # already auto-approving RO — the nudge would be noise
        t = targets.get(burst["target_server_id"])
        alias = t.alias if t else f"target #{burst['target_server_id']}"
        blocks = ro_window.nudge_blocks(
            count=burst["count"],
            window_min=cfg.get_int("ro_burst_window_min", 10),
            window_minutes=cfg.get_int("ro_window_minutes", 60),
            target_alias=alias,
            target_server_id=burst["target_server_id"],
            database_name=burst["database_name"],
            has_active_grant=False,
        )
        notifications.dm_requester(
            client, principal_id,
            text="You've run several read queries — here's a faster path.",
            blocks=blocks,
        )
    except Exception:
        log.exception("ro-burst nudge DM failed for %s", principal_id)


def _maybe_save_template(client: WebClient, user: dict, parsed: dict,
                         target_id: int, database: str) -> None:
    """If the user filled the "Save as template" field, persist it.
    Validates the name; surfaces validation errors as an ephemeral
    DM so the user knows the run succeeded but the bookmark didn't.
    Best-effort — never raises into the submit path."""
    name = parsed.get("template_name")
    if not name:
        return
    err = templates.name_error(name)
    if err:
        try:
            notifications.dm_requester(
                client, user["id"],
                f":warning: Request submitted, but the template wasn't "
                f"saved: {err}",
            )
        except Exception:
            log.exception("template-name-error DM failed")
        return
    try:
        templates.save(
            owner_slack_id=user["id"],
            name=name,
            query=parsed["query"],
            target_server_id=target_id,
            database_name=database,
            is_shared=bool(parsed.get("template_share")),
        )
    except Exception:
        log.exception("template save failed for user=%s name=%s",
                      user.get("id"), name)


def _maybe_save_favorite(client: WebClient, user: dict, parsed: dict,
                         target_id: int, database: str) -> None:
    """If the user ticked the 'favorite this query' checkbox, star it.
    Best-effort — never raises into the submit path."""
    if not parsed.get("favorite"):
        return
    try:
        favorites.add(
            principal_id=user["id"],
            query=parsed["query"],
            target_server_id=target_id,
            database_name=database,
        )
    except Exception:
        log.exception("favorite save failed for user=%s", user.get("id"))


# ===========================================================================
# Batch submission handler — `/sql batch`
# ===========================================================================


def _resolve_schedule(
    sched_date: str | None,
    sched_time: str | None,
    user_id: str,
) -> tuple[datetime | None, str | None, str | None]:
    """Parse the modal's date/time pickers in the user's local tz and
    return (utc_dt | None, error_block_id | None, error_text | None).
    Empty inputs → (None, None, None). The caller treats a non-None
    error as a modal-validation failure."""
    if not sched_date and not sched_time:
        return None, None, None
    if not (sched_date and sched_time):
        return (
            None,
            modal.BATCH_B_SCHEDULE_DATE if not sched_date else modal.BATCH_B_SCHEDULE_TIME,
            "Provide both date and time, or leave both empty.",
        )
    user_tz_name = profile_sync.lookup_tz(user_id) or "UTC"
    try:
        user_tz = ZoneInfo(user_tz_name)
    except ZoneInfoNotFoundError:
        user_tz = timezone.utc
    try:
        local_dt = datetime.fromisoformat(f"{sched_date}T{sched_time}:00").replace(tzinfo=user_tz)
        sched_for = local_dt.astimezone(timezone.utc)
    except ValueError:
        return None, modal.BATCH_B_SCHEDULE_DATE, "Invalid date or time."

    max_days = cfg.get_int("max_schedule_days", 7)
    if max_days <= 0:
        return None, modal.BATCH_B_SCHEDULE_DATE, "Scheduling is disabled. Leave date and time empty."
    now = datetime.now(timezone.utc)
    if sched_for <= now:
        return None, modal.BATCH_B_SCHEDULE_TIME, "Scheduled time must be in the future (UTC)."
    if sched_for > now + timedelta(days=max_days):
        return None, modal.BATCH_B_SCHEDULE_DATE, f"Scheduled time exceeds the {max_days}-day max."
    return sched_for, None, None


def _validate_batch_item(
    *,
    user_id: str,
    raw_item: dict,
    bundle_justification: str | None,
) -> tuple[dict | None, dict]:
    """Per-item validation. Returns (validated_item | None, errors).
    `errors` is a dict mapping block_id → message and is empty on success.

    The validated_item is a `bundles.BundleItem` ready for INSERT (with
    `explain_plan = None` — pre-flight is intentionally skipped on the
    batch path to keep submit under Slack's 3-second ack deadline; the
    executor surfaces real errors at run time).

    Block_ids in the returned errors map carry the per-item suffix the
    modal renders (`blk_b_query_3`), so Slack highlights the right row.
    """
    errors: dict[str, str] = {}
    idx = raw_item.get("_index", 1)
    b_server = f"{modal.BATCH_B_SERVER}_{idx}"
    b_database = f"{modal.BATCH_B_DATABASE}_{idx}"
    b_query = f"{modal.BATCH_B_QUERY}_{idx}"

    target_id = raw_item.get("target_server_id")
    if not target_id:
        errors[b_server] = "Pick a target server."
        return None, errors

    query = (raw_item.get("query") or "").strip()
    if not query:
        errors[b_query] = "Provide the SQL query for this item."
        return None, errors

    min_len = cfg.get_int("min_query_length", 6)
    if len(query) < min_len:
        errors[b_query] = f"Query must be at least {min_len} characters."
        return None, errors

    # Resolve the target first so the safety pass uses its engine (T-SQL
    # dialect + blocklist for a SQL Server target, read-only short-circuit
    # for a read-only engine).
    target = targets.get(target_id)
    if target is None:
        errors[b_server] = "Selected server is no longer available."
        return None, errors

    safety = query_safety.analyze(query, engine=target.engine)
    if safety.blocked:
        errors[b_query] = " ".join(safety.blockers)[:3000]
        return None, errors

    required_mode = query_safety.required_mode(query, engine=target.engine)

    grant = teams.effective_grant_for_user(user_id, target_id)
    if grant is None:
        errors[b_server] = "You are not authorized to query this server."
        return None, errors

    database = raw_item.get("database_name") or target.default_database

    allowed_dbs = grant["allowed_databases"]
    if allowed_dbs is not None and database not in allowed_dbs:
        sample = ", ".join(f"`{d}`" for d in sorted(allowed_dbs)[:8])
        more = "" if len(allowed_dbs) <= 8 else f" (+{len(allowed_dbs) - 8} more)"
        errors[b_database] = (
            f"You don't have access to database `{database}` on this "
            f"server. Allowed: {sample}{more}."
        )
        return None, errors

    # Tier for THIS database, not the target-wide max (see the cross-product note in
    # core_submit) — fail closed if nothing covers it.
    rank = {"ro": 0, "rw": 1, "ddl": 2}
    granted_mode = teams.effective_mode_for_database(user_id, target_id, database)
    if granted_mode is None or rank[required_mode] > rank[granted_mode]:
        if required_mode == "rw":
            errors[b_query] = (
                f"You don't have *write* access on `{target.alias}`. "
                "This is a read-only grant."
            )
        else:
            errors[b_query] = (
                f"You don't have *DDL* access on `{target.alias}` "
                "(CREATE/ALTER/DROP/TRUNCATE/VACUUM/...)."
            )
        return None, errors

    # Justification rule for write/DDL items: bundle-level justification
    # must be set. We flag the bundle field if it's empty.
    if required_mode != "ro" and not bundle_justification:
        errors[modal.BATCH_B_JUSTIFICATION] = (
            f"Justification is required because item #{idx} is a "
            f"{required_mode.upper()} query."
        )
        return None, errors

    # Duplicate guard — same per-item rule as single-shot.
    dup = db.fetch_one(
        "SELECT id, status FROM requests "
        "WHERE requester_slack_id = %s "
        "  AND target_server_id   = %s "
        "  AND database_name      = %s "
        "  AND query              = %s "
        "  AND status IN ('pending','changes_requested','approved',"
        "                 'scheduled','executing') "
        "ORDER BY id DESC LIMIT 1",
        (user_id, target_id, database, query),
    )
    if dup is not None:
        errors[b_query] = (
            f"You already have an active request (#{dup['id']}, "
            f"status={dup['status']}) with the same query on this "
            "target+database."
        )
        return None, errors

    return (
        {
            "target_server_id": target_id,
            "target_alias": target.alias,
            "database_name": database,
            "query": query,
            "wants_result": bool(raw_item.get("wants_result")),
            "result_format": raw_item.get("result_format") or "csv",
            "required_mode": required_mode,
            # Pre-flight intentionally skipped on the batch path; see the
            # docstring of _validate_batch_item.
            "explain_plan": None,
        },
        errors,
    )


def handle_batch_submission(ack: Ack, body: dict, client: WebClient) -> None:
    """Submit the batch modal. All items either succeed together or the
    modal stays open with per-row errors (Slack's validation_errors
    response action). Single transaction inserts the bundle parent row
    plus every item plus their audit log rows."""
    ack = _ack_logging(ack, body, "batch")
    user = body["user"]

    if not bundles.is_enabled():
        ack({"response_action": "errors",
             "errors": {modal.BATCH_B_JUSTIFICATION: "Batch mode is disabled."}})
        return

    if _kill_switch_on():
        ack({"response_action": "errors",
             "errors": {modal.BATCH_B_JUSTIFICATION: _kill_switch_message()}})
        return

    parsed = modal.parse_batch_submission(body["view"])
    raw_items = parsed["items"]
    if not raw_items:
        ack({"response_action": "errors",
             "errors": {modal.BATCH_B_JUSTIFICATION: "Add at least one item."}})
        return

    # Schedule resolution (per-bundle).
    sched_for, sched_err_block, sched_err_text = _resolve_schedule(
        parsed["schedule_date"], parsed["schedule_time"], user["id"],
    )
    if sched_err_block:
        ack({"response_action": "errors",
             "errors": {sched_err_block: sched_err_text}})
        return

    # Rate limit — count all items at once. Admins exempt (same rule as
    # single-shot path).
    if not admins.is_admin(user["id"]):
        max_open = cfg.get_int("max_open_requests_per_user", 5)
        open_count = requesters.open_request_count(user["id"])
        if open_count + len(raw_items) > max_open:
            ack({"response_action": "errors",
                 "errors": {modal.BATCH_B_JUSTIFICATION:
                            f"This batch ({len(raw_items)} items) would put you "
                            f"at {open_count + len(raw_items)} in-flight requests, "
                            f"over the {max_open} cap. Wait for some to complete, "
                            f"or remove items."}})
            return

    # Per-item validation. Tag each raw item with its 1-based index so
    # _validate_batch_item can write per-row block_ids into the errors map.
    all_errors: dict[str, str] = {}
    validated_items: list[bundles.BundleItem] = []
    for i, raw in enumerate(raw_items, start=1):
        raw_with_idx = dict(raw)
        raw_with_idx["_index"] = i
        validated, errs = _validate_batch_item(
            user_id=user["id"],
            raw_item=raw_with_idx,
            bundle_justification=parsed["justification"],
        )
        if errs:
            all_errors.update(errs)
            continue
        if validated is not None:
            validated_items.append(validated)

    if all_errors:
        ack({"response_action": "errors", "errors": all_errors})
        return

    # Global require_justification still applies to RO-only batches.
    if cfg.get_bool("require_justification", False) and not parsed["justification"]:
        ack({"response_action": "errors",
             "errors": {modal.BATCH_B_JUSTIFICATION:
                        "Justification is required."}})
        return

    ack()

    # Per-item auto-approve decision — same logic as single-shot:
    # cover at submit time AND at scheduled run time (if scheduled).
    aa_grants: list[dict | None] = []
    aa_expired_warning_items: list[int] = []   # 1-based positions
    for i, vi in enumerate(validated_items, start=1):
        g = auto_approve.effective_grant(
            user["id"], vi["required_mode"],
            target_server_id=vi["target_server_id"],
            database_name=vi["database_name"],
        )
        if g is not None and sched_for is not None:
            g_at_sched = auto_approve.effective_grant(
                user["id"], vi["required_mode"],
                target_server_id=vi["target_server_id"],
                database_name=vi["database_name"],
                at_time=sched_for,
            )
            if g_at_sched is None:
                aa_expired_warning_items.append(i)
                g = None
        aa_grants.append(g)

    # Single transaction: bundle + N items + per-item audit rows +
    # per-item auto-approve transitions where applicable.
    with db.transaction() as cur:
        result = bundles.insert_bundle_with_items(
            cur,
            requester_slack_id=user["id"],
            requester_name=user.get("name") or user.get("username"),
            justification=parsed["justification"],
            scheduled_for=sched_for,
            items=validated_items,
        )
        for item, row, grant in zip(validated_items,
                                    result["item_rows"], aa_grants):
            audit.log_in(cur, row["id"], user["id"], user.get("name"),
                         "submitted", {
                             "bundle_id": result["bundle_id"],
                             "position": row["position"],
                             "target_alias": item["target_alias"],
                             "database": item["database_name"],
                             "wants_result": item["wants_result"],
                             "auto_approved": grant is not None,
                         })
            if grant is not None:
                new_status = "scheduled" if sched_for is not None else "approved"
                cur.execute(
                    "UPDATE requests SET status = %s, "
                    " decided_by_slack_id = %s, decided_by_name = %s, "
                    " decision_reason = %s, decided_at = NOW() "
                    "WHERE id = %s "
                    "RETURNING id, requester_slack_id, requester_name, "
                    "          target_server_id, database_name, query, "
                    "          wants_result, justification, status, "
                    "          scheduled_for, decided_by_slack_id, "
                    "          decided_by_name, bundle_id, position",
                    (new_status, auto_approve.AUTO_DECIDED_BY,
                     auto_approve.decided_by_name_for(grant),
                     auto_approve.decided_by_name_for(grant),
                     row["id"]),
                )
                fresh = cur.fetchone()
                if fresh is not None:
                    # Overwrite the row in result["item_rows"] so the
                    # post-txn loops see the new status / decided cols.
                    for k, v in fresh.items():
                        row[k] = v
                audit.log_in(cur, row["id"], auto_approve.AUTO_DECIDED_BY,
                             None, "auto_approved", {
                                 "grant_id": grant["id"],
                                 "max_tier": grant["max_tier"],
                                 "scheduled_for": str(sched_for) if sched_for else None,
                             })

    bundle_id = result["bundle_id"]
    item_count = len(validated_items)

    # Risk hints for the admin DM. Pre-flight EXPLAIN is skipped during
    # per-item validation (to stay under Slack's 3s ack deadline), so we
    # run it here — AFTER ack() — once per pending item and persist
    # risk_summary. The bundle admin DM (rendered below by
    # notify_admins_bundle) reads it back. Only pending items need it
    # (auto-approved ones skip admin review), so we don't delay their
    # dispatch. Best-effort: any item that fails EXPLAIN just has no hint.
    if pre_flight.is_enabled():
        for item, row, grant in zip(validated_items, result["item_rows"], aa_grants):
            if grant is not None:
                continue  # auto-approved — no admin DM, skip the EXPLAIN
            q = item["query"]
            if not pre_flight.is_explainable(q):
                continue
            try:
                rs = None
                if item["required_mode"] == "rw":
                    # UPDATE/DELETE/INSERT: affected-row estimate (planned,
                    # not run) — same hint the single-submit path adds.
                    rs = pre_flight.explain_write_estimate(
                        item["target_server_id"], item["database_name"],
                        item["required_mode"], q,
                    )
                else:
                    ok, _err, plan = pre_flight.explain(
                        item["target_server_id"], item["database_name"],
                        item["required_mode"], q,
                    )
                    if ok and plan is not None:
                        rs = pre_flight.risk_summary_text(plan)
                if rs:
                    db.execute(
                        "UPDATE requests SET risk_summary = %s WHERE id = %s",
                        (rs, row["id"]),
                    )
            except Exception:
                log.exception("batch risk hint failed for request %s", row["id"])

    # Auto-approved items get an admin FYI DM (single line per item),
    # and the immediate ones dispatch to the executor right now.
    auto_items = [(item, row, g) for item, row, g
                  in zip(validated_items, result["item_rows"], aa_grants)
                  if g is not None]
    pending_items = [row for row, g in zip(result["item_rows"], aa_grants)
                     if g is None]
    if auto_items:
        _dm_admins_bundle_auto_approved(client, bundle_id, auto_items)
        for item, row, g in auto_items:
            if sched_for is None:
                executor.submit(row, client)
        # If scheduled, the existing scheduler thread picks the rows
        # up at the right time — no extra dispatch needed.

    # If there are still pending (non-auto) items, fan out the
    # normal bundle DM so admins can approve / reject them.
    if pending_items:
        if not admins.list_active():
            notifications.dm_requester(
                client, user["id"],
                f":warning: *SQL batch `B#{bundle_id}` ({len(pending_items)} "
                f"item(s)) saved* but no admins are configured to approve them.",
            )
        else:
            notifications.notify_admins_bundle(client, bundle_id)

    sched_line = ""
    if sched_for is not None:
        sched_line = f"\n*Scheduled for:* `{sched_for:%Y-%m-%d %H:%M UTC}`"
    item_ids = ", ".join(f"#{r['id']}" for r in result["item_rows"])
    auto_count = len(auto_items)
    pending_count = len(pending_items)
    summary_bits: list[str] = []
    if auto_count:
        summary_bits.append(f"{auto_count} auto-approved")
    if pending_count:
        summary_bits.append(f"{pending_count} waiting for admin approval")
    summary = ", ".join(summary_bits) if summary_bits else "submitted"

    expired_note = ""
    if aa_expired_warning_items:
        items_str = ", ".join(f"#{i}" for i in aa_expired_warning_items)
        expired_note = (
            f"\n:warning: _Items {items_str}: your auto-approve grant "
            "expires before the scheduled run time, so admin approval "
            "is required._"
        )

    notifications.dm_requester(
        client, user["id"],
        f":package: *SQL batch `B#{bundle_id}` submitted* "
        f"with {item_count} item(s) ({item_ids}) — {summary}."
        + sched_line + expired_note,
    )


def _dm_admins_bundle_auto_approved(
    client: WebClient,
    bundle_id: int,
    auto_items: list[tuple[dict, dict, dict]],
) -> None:
    """One FYI DM per active admin summarising every auto-approved
    item in this bundle. Each item gets a metadata line + an inline
    query preview (truncated). Quieter than per-item DMs (we'd
    otherwise spam 5 messages for a 5-item batch). No buttons."""
    requester = auto_items[0][1]["requester_slack_id"]
    header = (
        f":zap: *Auto-approved* {len(auto_items)} item(s) in batch "
        f"`B#{bundle_id}` from <@{requester}>:"
    )
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]
    fallback_lines: list[str] = []
    for item, row, grant in auto_items:
        meta = (
            f"• item #{row['position']}: `{item['target_alias']}/"
            f"{item['database_name']}` "
            f"({grant['max_tier'].upper()} via grant #{grant['id']}) "
            f"→ request #{row['id']}"
        )
        fallback_lines.append(meta)
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": meta}]})
        blocks.append(notifications.query_preview_block(item.get("query") or ""))
    # Slack message cap is 50 blocks; 5 items × 2 blocks + header = 11 → safe.
    fallback = header + "\n" + "\n".join(fallback_lines)
    overrides = notifications.display_overrides()
    for admin in admins.list_active():
        try:
            opened = client.conversations_open(users=admin["slack_user_id"])
            notifications._post(client,
                channel=opened["channel"]["id"],
                text=fallback,
                blocks=blocks,
                **overrides,
            )
        except Exception:
            log.exception(
                "bundle auto-approve FYI DM failed for admin %s on bundle %s",
                admin["slack_user_id"], bundle_id,
            )


def _load_request(request_id: int) -> dict | None:
    return db.fetch_one(
        "SELECT id, requester_slack_id, requester_name, target_server_id, "
        "       database_name, query, wants_result, justification, status, "
        "       decided_by_slack_id, decided_by_name "
        "FROM requests WHERE id = %s",
        (request_id,),
    )


def _guard_admin(
    ack: Ack,
    client: WebClient,
    body: dict,
    request_id: int | None = None,
) -> bool:
    """Block non-admins. When `request_id` is supplied, additionally
    enforce admins.can_approve(admin, request) — scope-based RBAC.
    NULL on all scope columns = wildcard (super admin, current
    default). Non-NULL columns narrow the admin's authority."""
    user_id = body["user"]["id"]
    if not admins.is_admin(user_id):
        ack()
        notifications.dm_requester(
            client, user_id,
            ":no_entry: You are not an authorized admin for the SQL bot.",
        )
        return False
    if request_id is not None:
        req = db.fetch_one(
            "SELECT id, query, target_server_id, requester_slack_id "
            "FROM requests WHERE id = %s",
            (request_id,),
        )
        if req is not None and not admins.can_approve(user_id, req):
            ack()
            notifications.dm_requester(
                client, user_id,
                ":no_entry: This request is outside your admin scope "
                "(tier / target / team restriction). Ask another admin "
                "with broader scope to handle it.",
            )
            return False
    profile_sync.maybe_backfill_user_profile(client, user_id)
    return True


def _admin_in_scope(admin_id: str, *, tier: str, target_server_id,
                    requester_slack_id) -> bool:
    """Scope check for operational approvals that are NOT a query request —
    access-request grants, CSV imports, RO-window grants. These
    hand out access (or DDL execution), so a scoped admin must be held to the
    same max_tier + target + team scope as a query approval. Reuses
    admins.can_approve by shaping the operation as a request-like dict."""
    return admins.can_approve(admin_id, {
        "required_tier": tier,
        "target_server_id": target_server_id,
        "requester_slack_id": requester_slack_id,
    })


_REQUEST_RETURNING = core_submit.REQUEST_RETURNING


def _is_bundle_item(request: dict | None) -> bool:
    """A single `requests` row that belongs to a `/sql batch` submission.
    For bundle items we suppress per-item admin DM updates + per-item
    requester DMs + the per-item rating prompt — the bundle-level
    notifications cover the user instead."""
    return bool(request and request.get("bundle_id"))


def handle_approve(ack: Ack, body: dict, client: WebClient) -> None:
    request_id = int(body["actions"][0]["value"])
    if not _guard_admin(ack, client, body, request_id=request_id):
        return
    ack()
    user = body["user"]
    outcome = core_decide.decide(request_id, "approve",
                                 by_id=user["id"], by_name=user.get("name"))
    if outcome is None:
        notifications.dm_requester(
            client, user["id"],
            f"Request `#{request_id}` has already been decided.")
        return
    core_decide.apply_effects(client, outcome)

def handle_reject(ack: Ack, body: dict, client: WebClient) -> None:
    request_id = body["actions"][0]["value"]
    if not _guard_admin(ack, client, body, request_id=int(request_id)):
        return
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "reject_modal",
            "private_metadata": request_id,
            "title": {"type": "plain_text", "text": "Reject request"},
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
        },
    )


def handle_request_changes(ack: Ack, body: dict, client: WebClient) -> None:
    request_id = body["actions"][0]["value"]
    if not _guard_admin(ack, client, body, request_id=int(request_id)):
        return
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "changes_modal",
            "private_metadata": request_id,
            "title": {"type": "plain_text", "text": "Request changes"},
            "submit": {"type": "plain_text", "text": "Send"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "reason_block",
                    "label": {"type": "plain_text", "text": "What needs to change?"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "reason_input",
                        "multiline": True,
                    },
                }
            ],
        },
    )


def _decision_modal_reason(body: dict) -> tuple[int, str]:
    request_id = int(body["view"]["private_metadata"])
    reason = body["view"]["state"]["values"]["reason_block"]["reason_input"]["value"].strip()
    return request_id, reason


def handle_reject_submission(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    request_id, reason = _decision_modal_reason(body)
    user = body["user"]
    outcome = core_decide.decide(request_id, "reject", by_id=user["id"],
                                 by_name=user.get("name"), reason=reason)
    if outcome is not None:
        core_decide.apply_effects(client, outcome)

def handle_changes_submission(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    request_id, reason = _decision_modal_reason(body)
    user = body["user"]
    outcome = core_decide.decide(request_id, "changes", by_id=user["id"],
                                 by_name=user.get("name"), reason=reason)
    if outcome is not None:
        core_decide.apply_effects(client, outcome)

# =============================================================================
# Cancel a scheduled request (button on both user DM and admin DMs)
# =============================================================================

def handle_cancel_scheduled(ack: Ack, body: dict, client: WebClient) -> None:
    """Cancel a pending scheduled execution. Allowed for the requester (own
    request only) and for any admin. Updates both admin DMs and the user's
    scheduled DM in lockstep so the [Cancel] button disappears everywhere."""
    ack()
    user_id = body["user"]["id"]
    user_name = body["user"].get("name")
    request_id = int(body["actions"][0]["value"])

    req = db.fetch_one(
        f"SELECT {_REQUEST_RETURNING} FROM requests WHERE id = %s",
        (request_id,),
    )
    if req is None:
        return

    is_admin = admins.is_admin(user_id)
    is_owner = req["requester_slack_id"] == user_id
    if not (is_admin or is_owner):
        notifications.dm_requester(
            client, user_id,
            f":no_entry: You can only cancel your own scheduled requests.",
        )
        return

    with db.transaction() as cur:
        cur.execute(
            f"UPDATE requests SET status = 'cancelled', "
            f" decision_reason = COALESCE(decision_reason, '') || "
            f"   CASE WHEN decision_reason IS NULL OR decision_reason = '' "
            f"        THEN %s ELSE ' / ' || %s END, "
            f" decided_at = NOW() "
            f"WHERE id = %s AND status = 'scheduled' "
            f"RETURNING {_REQUEST_RETURNING}",
            (
                f"cancelled by <@{user_id}>",
                f"cancelled by <@{user_id}>",
                request_id,
            ),
        )
        updated = cur.fetchone()
        if updated is None:
            # Either already executed or someone else cancelled.
            notifications.dm_requester(
                client, user_id,
                f"Request `#{request_id}` is no longer scheduled — it may have "
                f"already started executing or been cancelled.",
            )
            return
        audit.log_in(cur, request_id, user_id, user_name, "cancelled")

    if _is_bundle_item(updated):
        # Bundle items: bundle DM gets refreshed; suppress the
        # single-item admin DM update + scheduled-DM update + rating
        # prompt. Bundle summary DM will land once the bundle reaches
        # a terminal state.
        notifications.update_bundle_admin_dms(client, updated["bundle_id"])
    else:
        notifications.update_all_admin_messages(
            client, updated,
            f":no_entry: Cancelled by <@{user_id}> before execution",
        )
        notifications.update_user_scheduled_dm(
            client, updated,
            f":no_entry: *SQL query `#{request_id}` cancelled by <@{user_id}>* — "
            f"the scheduled execution was skipped.",
        )
        ratings.maybe_prompt(client, updated)

    # If the canceller wasn't the owner, send the owner a heads-up.
    # Bundle items: skip — the bundle summary DM will cover this once
    # the whole bundle reaches a terminal state.
    if not is_owner and not _is_bundle_item(updated):
        notifications.dm_requester(
            client, updated["requester_slack_id"],
            f":no_entry: Your scheduled SQL query `#{request_id}` was "
            f"cancelled by admin <@{user_id}> before execution.\n"
            + notifications.request_context_md(updated),
        )


def handle_cancel_request(ack: Ack, body: dict, client: WebClient) -> None:
    """Requester withdraws their own still-pending request via the
    [Cancel request] button on the submit-confirm card. Owner-only (the
    button lives in their DM, but ownership is re-checked anyway; admins
    have Reject for the same effect). The pending→cancelled transition is
    atomic, so a race with an admin's Approve resolves cleanly to
    whichever side commits first."""
    ack()
    user_id = body["user"]["id"]
    user_name = body["user"].get("name")
    try:
        request_id = int(body["actions"][0]["value"])
    except (KeyError, IndexError, ValueError, TypeError):
        return

    req = db.fetch_one(
        f"SELECT {_REQUEST_RETURNING} FROM requests WHERE id = %s",
        (request_id,),
    )
    if req is None:
        return
    if req["requester_slack_id"] != user_id:
        notifications.dm_requester(
            client, user_id,
            ":no_entry: You can only cancel your own requests.",
        )
        return

    with db.transaction() as cur:
        cur.execute(
            f"UPDATE requests SET status = 'cancelled', "
            f" decision_reason = 'withdrawn by requester', "
            f" decided_at = NOW() "
            f"WHERE id = %s AND status = 'pending' "
            f"RETURNING {_REQUEST_RETURNING}",
            (request_id,),
        )
        updated = cur.fetchone()
        if updated is None:
            # Raced: an admin decided (or it moved on) between the click
            # and this update. The card refreshes via that flow.
            notifications.dm_requester(
                client, user_id,
                f"Request `#{request_id}` is no longer pending — an admin "
                f"may have already acted on it. Check its latest status DM.",
            )
            return
        audit.log_in(cur, request_id, user_id, user_name, "cancelled",
                     {"phase": "pending", "by": "requester"})

    log.info("Request %s withdrawn by requester %s", request_id, user_id)

    if _is_bundle_item(updated):
        notifications.update_bundle_admin_dms(client, updated["bundle_id"])
        return

    # Admin cards lose their Approve/Reject buttons; the requester's own
    # card swaps the [Cancel request] button for a terminal status line.
    notifications.update_all_admin_messages(
        client, updated,
        ":no_entry: Withdrawn by the requester before a decision",
    )
    notifications.update_requester_card(
        client, updated,
        status_emoji=":no_entry:",
        status_text="Cancelled — you withdrew this request",
    )


# =============================================================================
# Access-request flow
# =============================================================================

def handle_open_access_request(ack: Ack, body: dict, client: WebClient) -> None:
    """[Request access] button on the /sql-blocked ephemeral. Opens the
    access-request modal — unless the kill switch is on, in which case
    we DM the user the same downtime message."""
    ack()
    if _kill_switch_on():
        notifications.dm_requester(
            client, body["user"]["id"], _kill_switch_message(),
        )
        return
    client.views_open(
        trigger_id=body["trigger_id"],
        view=access.build_request_modal(),
    )


def handle_access_target_options(ack: Ack, payload: dict) -> None:
    """Typeahead handler for the access-request modal's target select.
    Returns ALL enabled targets — the user is by definition asking for
    something they can't reach today."""
    query = (payload.get("value") or "").strip()
    ack({"options": access.options_for_targets(query)})


def handle_access_request_submission(ack: Ack, body: dict, client: WebClient) -> None:
    user = body["user"]

    if _kill_switch_on():
        ack({
            "response_action": "errors",
            "errors": {access.B_REASON: _kill_switch_message()},
        })
        return

    parsed = access.parse_modal_submission(body["view"]["state"])

    if not parsed["reason"] or len(parsed["reason"]) < 5:
        ack({
            "response_action": "errors",
            "errors": {access.B_REASON: "Please provide a meaningful reason (at least 5 characters)."},
        })
        return

    # Per (user, target, query) only one pending allowed.
    existing = access_requests.find_pending_for(
        user["id"], parsed["target_server_id"], parsed["attempted_query"]
    )
    if existing is not None:
        ack({
            "response_action": "errors",
            "errors": {
                access.B_REASON: (
                    f"You already have a pending access request "
                    f"(#{existing['id']}, opened {existing['created_at']:%Y-%m-%d}). "
                    f"Wait for it to be decided before submitting another for the "
                    f"same target + query."
                )
            },
        })
        return

    target = targets.get(parsed["target_server_id"])
    if target is None:
        ack({
            "response_action": "errors",
            "errors": {access.B_TARGET: "Selected target no longer exists."},
        })
        return

    ack()

    new_row = access_requests.create(
        principal_id=user["id"],
        name=user.get("name") or user.get("username"),
        target_server_id=parsed["target_server_id"],
        database_name=parsed["database_name"],
        attempted_query=parsed["attempted_query"],
        reason=parsed["reason"],
    )
    if new_row is None:
        # Lost a race with another submission — tell the user gently.
        notifications.dm_requester(
            client, user["id"],
            ":warning: Looks like an identical pending request exists already. "
            "No new request was created.",
        )
        return

    # Fan out to admins; record each DM so we can update them in lockstep
    # when the request is decided.
    active = admins.list_active()
    if not active:
        notifications.dm_requester(
            client, user["id"],
            ":warning: Your access request was saved (#"
            f"{new_row['id']}) but there are no admins configured to review it. "
            "Contact the DBA team out-of-band.",
        )
        return

    access.fan_out_admin_dms(client, new_row, target)

    notifications.dm_requester(
        client, user["id"],
        f":hourglass_flowing_sand: *Access request `#{new_row['id']}` submitted* — admins will review.\n"
        + access.access_context_md(new_row),
    )


def _update_all_access_admin_messages(
    client: WebClient,
    access_request_id: int,
    target,
    status_line: str,
) -> None:
    req = access_requests.get(access_request_id)
    if req is None:
        return
    blocks = access.resolved_admin_dm_blocks(req, target, status_line)
    for r in access_requests.list_admin_dms(access_request_id):
        try:
            notifications._update(client,
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


def handle_access_approve(ack: Ack, body: dict, client: WebClient) -> None:
    if not _guard_admin(ack, client, body):
        return
    access_request_id = int(body["actions"][0]["value"])
    user = body["user"]
    # Scope check: _guard_admin only enforces scope for query
    # requests. An access request grants a tier on a target, so a scoped
    # admin may approve it only within their max_tier + target + team scope.
    ar = access_requests.get(access_request_id)
    if ar is not None and not _admin_in_scope(
            user["id"], tier=access_requests.requested_tier_of(ar),
            target_server_id=ar["target_server_id"],
            requester_slack_id=ar["requester_slack_id"]):
        ack()
        notifications.dm_requester(
            client, user["id"],
            f":no_entry: Access request `#{access_request_id}` is outside your "
            "admin scope (tier / target / team). Ask an admin with broader "
            "scope to handle it.")
        return
    ack()
    updated = access_requests.decide(
        access_request_id, "approved",
        decided_by_slack_id=user["id"],
        decided_by_name=user.get("name"),
        decision_reason=None,
    )
    if updated is None:
        notifications.dm_requester(
            client, user["id"],
            f"Access request `#{access_request_id}` has already been decided.",
        )
        return

    target = targets.get(updated["target_server_id"]) if updated["target_server_id"] else None
    # decide() auto-granted (or explains why not) — surface that on the card
    # so the admin knows whether any manual SQL is still needed.
    ag = updated.get("auto_grant") or {}
    if ag.get("applied"):
        dbs = ag.get("databases")
        db_txt = ", ".join(f"`{d}`" for d in dbs) if dbs else "_all databases_"
        grant_line = (f"\n:key: Granted automatically: *{(ag.get('mode') or 'ro').upper()}* "
                      f"on {db_txt}.")
        requester_note = "\n\nYou can now run `/sql` — your access is active."
    elif ag.get("reason") == "tier_conflict":
        grant_line = ("\n:warning: Auto-grant skipped: an active grant at a different "
                      f"tier (*{(ag.get('mode') or '?').upper()}*) already exists — "
                      "adjust it manually if intended.")
        requester_note = "\n\nA DBA will finalize your access shortly."
    elif ag.get("reason") == "no_target":
        grant_line = ("\n:warning: Auto-grant skipped: this server is not onboarded "
                      "as a target yet — onboard it, then grant manually.")
        requester_note = "\n\nA DBA will finalize your access shortly."
    else:
        grant_line = ""
        requester_note = "\n\nYou can now run `/sql`."
    _update_all_access_admin_messages(
        client, access_request_id, target,
        f":white_check_mark: Approved by <@{user['id']}>" + grant_line,
    )
    notifications.dm_requester(
        client, updated["requester_slack_id"],
        f":white_check_mark: *Access request `#{access_request_id}` approved* by <@{user['id']}>.\n"
        + access.access_context_md(updated)
        + requester_note,
    )


def handle_access_reject(ack: Ack, body: dict, client: WebClient) -> None:
    if not _guard_admin(ack, client, body):
        return
    ack()
    access_request_id = int(body["actions"][0]["value"])
    client.views_open(
        trigger_id=body["trigger_id"],
        view=access.build_reject_modal(access_request_id),
    )


def handle_access_reject_submission(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    access_request_id, reason = _decision_modal_reason(body)
    user = body["user"]
    updated = access_requests.decide(
        access_request_id, "rejected",
        decided_by_slack_id=user["id"],
        decided_by_name=user.get("name"),
        decision_reason=reason,
    )
    if updated is None:
        return
    target = targets.get(updated["target_server_id"]) if updated["target_server_id"] else None
    _update_all_access_admin_messages(
        client, access_request_id, target,
        f":x: Rejected by <@{user['id']}> — {reason}",
    )
    notifications.dm_requester(
        client, updated["requester_slack_id"],
        f":x: *Access request `#{access_request_id}` rejected* by <@{user['id']}>\n"
        + access.access_context_md(updated)
        + f"\n*Reason:* {reason}",
    )



# =============================================================================
# Rating flow — 1-5 buttons + optional feedback
# =============================================================================

def _ack_user_match(body: dict, request_id: int) -> bool:
    """Only the requester of the original request can rate it. Defends
    against another user clicking buttons in a shared channel (rare —
    these prompts go to DM — but cheap belt-and-suspenders)."""
    clicker = body.get("user", {}).get("id")
    row = db.fetch_one(
        "SELECT requester_slack_id FROM requests WHERE id = %s",
        (request_id,),
    )
    return bool(row and clicker and row["requester_slack_id"] == clicker)


def handle_rating_click(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    action = body["actions"][0]
    request_id = int(action["value"])
    rating = int(action["action_id"].removeprefix(ratings.ACTION_RATE_PREFIX))
    user_id = body["user"]["id"]

    if not _ack_user_match(body, request_id):
        return  # silent — wrong user clicking

    inserted = ratings.save(request_id, user_id, rating)
    if not inserted:
        # Already rated; still update the message so user gets feedback.
        existing = ratings.get(request_id) or {}
        rating = existing.get("rating", rating)

    # Update the prompt DM in place: replace prompt blocks with thanks.
    has_fb = bool((ratings.get(request_id) or {}).get("feedback_text"))
    notifications._update(client,
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=f"Rated {rating}/5",
        blocks=ratings.thanks_blocks(request_id, rating, has_feedback=has_fb),
        **notifications.display_overrides(),
    )


def handle_rating_skip(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    notifications._update(client,
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Rating skipped",
        blocks=ratings.skipped_blocks(),
        **notifications.display_overrides(),
    )


def handle_rating_add_feedback(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    request_id = int(body["actions"][0]["value"])
    rating_row = ratings.get(request_id)
    if rating_row is None:
        # Edge case: someone clicked feedback button without rating first.
        return
    # Stash both request_id and the message ts/channel of the prompt so
    # we can chat_update it after submit.
    metadata = json.dumps({
        "request_id": request_id,
        "channel": body["channel"]["id"],
        "message_ts": body["message"]["ts"],
    })
    view = ratings.feedback_modal(request_id, rating_row["rating"])
    view["private_metadata"] = metadata
    client.views_open(trigger_id=body["trigger_id"], view=view)


def handle_rating_feedback_submit(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    pm = json.loads(body["view"]["private_metadata"])
    request_id = int(pm["request_id"])
    feedback = (
        body["view"]["state"]["values"][ratings.FEEDBACK_BLOCK_ID]
                                       [ratings.FEEDBACK_INPUT_ID]
                                       .get("value") or ""
    ).strip()
    if not feedback:
        return
    ratings.add_feedback(request_id, feedback)

    rating_row = ratings.get(request_id)
    if rating_row:
        try:
            notifications._update(client,
                channel=pm["channel"],
                ts=pm["message_ts"],
                text=f"Rated {rating_row['rating']}/5 with feedback",
                blocks=ratings.thanks_blocks(
                    request_id,
                    rating_row["rating"],
                    has_feedback=True,
                ),
                **notifications.display_overrides(),
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to update rating prompt after feedback")


# =============================================================================
# DBA manual completion / failure for escalated DDL requests
# =============================================================================

def handle_dba_mark_completed(ack: Ack, body: dict, client: WebClient) -> None:
    """Admin clicks Mark-completed on an escalated DDL request after
    running it manually with elevated creds. Transitions
    awaiting_dba_manual → completed."""
    request_id = int(body["actions"][0]["value"])
    if not _guard_admin(ack, client, body, request_id=request_id):
        return
    ack()
    user = body["user"]
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'completed', completed_at = NOW(), "
            " decision_reason = COALESCE(decision_reason, '') || "
            f"   CASE WHEN decision_reason IS NULL OR decision_reason = '' "
            f"        THEN %s ELSE ' / ' || %s END "
            "WHERE id = %s AND status = 'awaiting_dba_manual' "
            f"RETURNING {_REQUEST_RETURNING}",
            (
                f"manually completed by <@{user['id']}>",
                f"manually completed by <@{user['id']}>",
                request_id,
            ),
        )
        updated = cur.fetchone()
        if updated is None:
            return  # raced; already closed
        audit.log_in(cur, request_id, user["id"], user.get("name"),
                     "completed_manually")

    if _is_bundle_item(updated):
        notifications.update_bundle_admin_dms(client, updated["bundle_id"])
        return
    notifications.update_all_admin_messages(
        client, updated,
        f":white_check_mark: Manually completed by <@{user['id']}> "
        f"(DDL ran out-of-band).",
    )
    notifications.dm_requester(
        client, updated["requester_slack_id"],
        f":white_check_mark: *SQL query `#{request_id}` completed* — "
        f"DBA ran it manually with elevated credentials.\n"
        + notifications.request_context_md(updated),
    )
    ratings.maybe_prompt(client, updated)


def handle_dba_mark_failed(ack: Ack, body: dict, client: WebClient) -> None:
    """Admin clicks Mark-failed. Opens a small modal asking for the
    failure reason (free text), then transitions
    awaiting_dba_manual → failed with the reason recorded."""
    request_id = body["actions"][0]["value"]
    if not _guard_admin(ack, client, body, request_id=int(request_id)):
        return
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "dba_failed_modal",
            "private_metadata": request_id,
            "title": {"type": "plain_text", "text": "Mark as failed"},
            "submit": {"type": "plain_text", "text": "Mark failed"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "reason_block",
                    "label": {"type": "plain_text",
                              "text": "What went wrong? (visible to requester)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "reason_input",
                        "multiline": True,
                        "max_length": 1000,
                    },
                }
            ],
        },
    )


def handle_dba_failed_submission(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    request_id = int(body["view"]["private_metadata"])
    reason = (
        body["view"]["state"]["values"]["reason_block"]["reason_input"]
            .get("value") or ""
    ).strip() or "(no reason given)"
    user = body["user"]
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'failed', completed_at = NOW(), "
            " error_message = %s "
            "WHERE id = %s AND status = 'awaiting_dba_manual' "
            f"RETURNING {_REQUEST_RETURNING}",
            (f"manual DBA execution failed: {reason}", request_id),
        )
        updated = cur.fetchone()
        if updated is None:
            return
        audit.log_in(cur, request_id, user["id"], user.get("name"),
                     "failed_manually", {"reason": reason})

    if _is_bundle_item(updated):
        notifications.update_bundle_admin_dms(client, updated["bundle_id"])
        return
    notifications.update_all_admin_messages(
        client, updated,
        f":x: Marked failed by <@{user['id']}> after manual attempt — {reason}",
    )
    notifications.dm_requester(
        client, updated["requester_slack_id"],
        f":x: *SQL query `#{request_id}` failed* during DBA manual "
        f"execution.\n"
        + notifications.request_context_with_query_md(updated)
        + f"\n*Reason:* {reason}",
    )
    ratings.maybe_prompt(client, updated)


# =============================================================================
# Bulk bundle decisions — "Approve / Reject all remaining (in scope)"
# =============================================================================


def _bundle_pending_items_in_scope(bundle_id: int, admin_id: str) -> list[dict]:
    """Pending items in a bundle that this admin's scope allows. Loads
    each item with the fields admins.can_approve() looks at."""
    rows = db.fetch_all(
        "SELECT id, query, target_server_id, requester_slack_id, "
        "       scheduled_for, bundle_id "
        "  FROM requests "
        " WHERE bundle_id = %s AND status = 'pending' "
        " ORDER BY position",
        (bundle_id,),
    )
    return [r for r in rows if admins.can_approve(admin_id, r)]


def handle_bundle_approve_all(ack: Ack, body: dict, client: WebClient) -> None:
    """[Approve all remaining (in scope)]. Walks the pending items the
    admin is allowed to act on and approves each one, mirroring the
    per-item handle_approve transition (status → approved or scheduled
    depending on bundle.scheduled_for, executor.submit for immediate
    items)."""
    if not _guard_admin(ack, client, body):
        return
    ack()
    user = body["user"]
    bundle_id = int(body["actions"][0]["value"])

    items = _bundle_pending_items_in_scope(bundle_id, user["id"])
    if not items:
        notifications.dm_requester(
            client, user["id"],
            f":eyes: No pending items in your scope on bundle B#{bundle_id}.",
        )
        return

    now = datetime.now(timezone.utc)
    immediate: list[dict] = []
    skipped = 0
    for item in items:
        sched = item.get("scheduled_for")
        deferred = sched is not None and sched > now
        new_status = "scheduled" if deferred else "approved"
        with db.transaction() as cur:
            cur.execute(
                f"UPDATE requests SET status = %s, "
                f" decided_by_slack_id = %s, decided_by_name = %s, "
                f" decided_at = NOW() "
                f"WHERE id = %s AND status = 'pending' "
                f"RETURNING {_REQUEST_RETURNING}",
                (new_status, user["id"], user.get("name"), item["id"]),
            )
            updated = cur.fetchone()
            if updated is None:
                # Raced with another admin click — skip.
                skipped += 1
                continue
            audit.log_in(cur, item["id"], user["id"], user.get("name"),
                         "approved",
                         {"bulk": True, "bundle_id": bundle_id,
                          "scheduled_for": str(sched) if sched else None,
                          "deferred": deferred})
        if not deferred:
            immediate.append(updated)

    # Refresh the bundle DM for everyone once at the end (cheaper than
    # re-rendering on every item).
    notifications.update_bundle_admin_dms(client, bundle_id)

    # Dispatch all the immediate approvals to the executor.
    for req in immediate:
        executor.submit(req, client)

    notifications.dm_requester(
        client, user["id"],
        f":white_check_mark: Approved {len(items) - skipped} item(s) in "
        f"bundle B#{bundle_id}"
        + (f" — {skipped} raced and were skipped." if skipped else "."),
    )


def handle_bundle_reject_all(ack: Ack, body: dict, client: WebClient) -> None:
    """[Reject all remaining (in scope)]. Opens a single reason-modal;
    on submit, every pending item in scope flips to rejected with the
    same reason."""
    if not _guard_admin(ack, client, body):
        return
    ack()
    bundle_id = body["actions"][0]["value"]
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "bundle_reject_modal",
            "private_metadata": str(bundle_id),
            "title": {"type": "plain_text", "text": "Reject bundle items"},
            "submit": {"type": "plain_text", "text": "Reject all"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {"type": "context",
                 "elements": [{"type": "mrkdwn",
                               "text": (f"Applies to every pending item in "
                                        f"*B#{bundle_id}* that's in your scope.")}]},
                {"type": "input",
                 "block_id": "reason_block",
                 "label": {"type": "plain_text", "text": "Reason"},
                 "element": {"type": "plain_text_input",
                             "action_id": "reason_input",
                             "multiline": True}},
            ],
        },
    )


def handle_bundle_reject_submission(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    bundle_id = int(body["view"]["private_metadata"])
    reason = (body["view"]["state"]["values"]["reason_block"]
                  ["reason_input"].get("value") or "").strip()
    user = body["user"]
    items = _bundle_pending_items_in_scope(bundle_id, user["id"])
    if not items:
        return
    rejected = 0
    for item in items:
        with db.transaction() as cur:
            cur.execute(
                f"UPDATE requests SET status = 'rejected', "
                f" decided_by_slack_id = %s, decided_by_name = %s, "
                f" decision_reason = %s, decided_at = NOW() "
                f"WHERE id = %s AND status = 'pending' "
                f"RETURNING {_REQUEST_RETURNING}",
                (user["id"], user.get("name"), reason, item["id"]),
            )
            updated = cur.fetchone()
            if updated is None:
                continue
            audit.log_in(cur, item["id"], user["id"], user.get("name"),
                         "rejected",
                         {"bulk": True, "bundle_id": bundle_id,
                          "reason": reason})
            rejected += 1

    notifications.update_bundle_admin_dms(client, bundle_id)
    notifications.dm_requester(
        client, user["id"],
        f":x: Rejected {rejected} item(s) in bundle B#{bundle_id} "
        f"— {reason or '(no reason)'}.",
    )


# ===========================================================================
# CSV import handlers (`/sql import`)
# ===========================================================================


def handle_import_server_options(ack: Ack, payload: dict, body: dict) -> None:
    """external_select options for the import modal's target picker."""
    user_id = body.get("user", {}).get("id", "")
    query = (payload or {}).get("value", "")
    ack(options=modal.options_for_targets(user_id, query))


def _download_csv_bytes(client: WebClient, file_id: str) -> bytes:
    """Fetch an uploaded CSV's raw bytes from Slack, capped at import_max_mb."""
    cap = cfg.get_int("import_max_mb", 50) * 1024 * 1024
    info = client.files_info(file=file_id)
    fm = info.get("file") or {}
    url = fm.get("url_private_download") or fm.get("url_private")
    if not url:
        raise RuntimeError("no download URL in files.info response")
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {cfg.ENV.slack_bot_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read(cap + 1)
    if len(raw) > cap:
        raise ValueError(f"CSV exceeds the {cap // 1024 // 1024} MB cap")
    return raw


def handle_import_submission(ack: Ack, body: dict, client: WebClient) -> None:
    """Validate the import modal (light checks in-ack), then process the
    upload in the background (download + parse + table check + notify)."""
    user = body["user"]
    uid = user["id"]
    parsed = modal.parse_import_submission(body["view"])

    def err(block, msg):
        ack({"response_action": "errors", "errors": {block: msg}})

    if not csv_import.is_enabled():
        return err(modal.B_IMPORT_FILE, "CSV import is currently disabled.")
    if _kill_switch_on():
        return err(modal.B_IMPORT_FILE, _kill_switch_message())
    if not csv_import.can_import(uid):
        return err(modal.B_IMPORT_FILE,
                   "You don't have import permission. Contact the DBA team.")
    if not parsed["target_server_id"]:
        return err(modal.B_IMPORT_SERVER, "Pick a target server.")
    if not parsed["file_id"]:
        return err(modal.B_IMPORT_FILE, "Attach a CSV file.")
    if not parsed["table_name"]:
        return err(modal.B_IMPORT_TABLE_NAME, "Provide a table name.")

    # Normalize/validate table name (single identifier, dba schema).
    seen: set[str] = set()
    norm_table = csv_import.normalize_column(parsed["table_name"], 0, seen)
    if not norm_table or norm_table.startswith("col_"):
        return err(modal.B_IMPORT_TABLE_NAME,
                   "Invalid table name. Use letters, digits, underscores.")

    # Target must be one the user can reach.
    grant = teams.effective_grant_for_user(uid, parsed["target_server_id"])
    if grant is None:
        return err(modal.B_IMPORT_SERVER, "You can't reach this target server.")
    target = targets.get(parsed["target_server_id"])
    if target is None:
        return err(modal.B_IMPORT_SERVER, "Selected server is unavailable.")

    # An import CREATEs a table and COPYs into it, and the executor runs it with
    # the target's DDL credential — so it must be authorized like DDL, per
    # database. "Has some grant on this server" was the whole check before, so a
    # user with RO on one database could create tables in another database on
    # the same server (executor.py's import path uses the DDL login regardless
    # of the requester's tier). Same resolver the SQL path uses, so the two
    # cannot drift.
    import_db = parsed["database"] or target.default_database
    db_mode = teams.effective_mode_for_database(
        uid, parsed["target_server_id"], import_db)
    if db_mode != "ddl":
        return err(
            modal.B_IMPORT_SERVER,
            f"A CSV import creates a table, so it needs a *DDL* grant on "
            f"`{target.alias}` / `{import_db}` — your grant there is "
            f"{(db_mode or 'none').upper()}. Ask the DBA team for a DDL grant "
            f"on that database, or import into one where you already have it.")

    ack()  # modal closes; heavy work runs in the background

    parsed["table_name"] = norm_table
    parsed["database"] = import_db
    try:
        _process_import(client, user, parsed, target)
    except Exception:
        log.exception("import processing failed for user %s", uid)
        try:
            notifications.dm_requester(
                client, uid,
                ":x: *CSV import failed to process.* Please try again or "
                "contact the DBA team.")
        except Exception:
            pass


def _process_import(client: WebClient, user: dict, parsed: dict, target) -> None:
    """Background: download CSV, parse, validate target table, insert the
    csv_imports row, and fan out the admin approval DM."""
    uid = user["id"]
    delimiter = csv_import.DELIMITERS.get(parsed["delimiter_key"], ",")
    table = parsed["table_name"]
    is_new = parsed["table_mode"] == modal.IMPORT_MODE_NEW

    # Download + parse.
    data = _download_csv_bytes(client, parsed["file_id"])
    pc = csv_import.parse_csv(data, delimiter)
    if pc.error:
        notifications.dm_requester(client, uid, f":x: *CSV import rejected:* {pc.error}")
        return

    # Table existence check against the dba schema (RO creds, info schema).
    import psycopg
    try:
        ro_user, ro_pw = targets.get_credentials(target.id, "ro")
        with psycopg.connect(host=target.host, port=target.port,
                             dbname=parsed["database"], user=ro_user, password=ro_pw,
                             sslmode="require", connect_timeout=10) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema='dba' AND table_name=%s", (table,))
            exists = cur.fetchone() is not None
            existing_cols = []
            if exists:
                cur.execute("SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='dba' AND table_name=%s", (table,))
                existing_cols = [r[0] for r in cur.fetchall()]
    except Exception as e:
        notifications.dm_requester(
            client, uid, f":x: *CSV import:* could not verify target table "
            f"`dba.{table}` — {str(e).splitlines()[0]}")
        return

    if is_new and exists:
        notifications.dm_requester(
            client, uid, f":x: *CSV import rejected:* `dba.{table}` already "
            f"exists. Use the 'existing table' option to append, or pick a new name.")
        return
    if not is_new and not exists:
        notifications.dm_requester(
            client, uid, f":x: *CSV import rejected:* `dba.{table}` does not "
            f"exist. Use the 'new table' option to create it.")
        return
    if not is_new:
        missing = [c for c in pc.columns if c not in existing_cols]
        if missing:
            notifications.dm_requester(
                client, uid, f":x: *CSV import rejected:* CSV columns "
                f"{', '.join('`'+m+'`' for m in missing)} are not in "
                f"`dba.{table}`. CSV header must be a subset of the table's columns.")
            return

    # Optional user-supplied column types (new-table only). Blank = all TEXT.
    column_defs = None
    if is_new and parsed.get("coldefs_text"):
        column_defs, cd_err = csv_import.parse_column_defs(
            parsed["coldefs_text"], len(pc.columns))
        if cd_err:
            notifications.dm_requester(
                client, uid, f":x: *CSV import rejected:* {cd_err}")
            return

    # Insert the import row, then write the CSV to disk under its id.
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO csv_imports "
            "(requester_slack_id, requester_name, target_server_id, database_name, "
            " table_name, is_new_table, unlogged, delimiter, columns, column_defs, "
            " row_count, byte_size, slack_file_id, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,'pending') "
            "RETURNING id",
            (uid, user.get("name") or user.get("username"), target.id, parsed["database"],
             table, is_new, parsed["unlogged"] if is_new else False, delimiter,
             json.dumps(pc.columns),
             json.dumps(column_defs) if column_defs else None,
             pc.row_count, pc.byte_size, parsed["file_id"]),
        )
        import_id = cur.fetchone()["id"]
        audit.log_in(cur, None, uid, user.get("name"), "import_submitted", {
            "import_id": import_id, "table": f"dba.{table}",
            "is_new_table": is_new, "row_count": pc.row_count,
            "target_alias": target.alias, "database": parsed["database"],
        })

    executor.IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = executor.IMPORT_DIR / f"import_{import_id}.csv"
    csv_path.write_bytes(data)
    db.execute("UPDATE csv_imports SET csv_file_path=%s WHERE id=%s",
               (str(csv_path), import_id))

    imp = db.fetch_one("SELECT * FROM csv_imports WHERE id=%s", (import_id,))
    if not admins.list_active():
        notifications.dm_requester(
            client, uid, f":warning: *CSV import `#{import_id}` saved* but no "
            f"admins are configured to approve it.")
        return
    notifications.notify_admins_import(client, imp, pc)
    notifications.dm_requester(
        client, uid,
        f":hourglass_flowing_sand: *CSV import `#{import_id}` submitted* — "
        f"{pc.row_count:,} rows into `dba.{table}`. Waiting for admin approval.")


def handle_import_approve(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    admin = body["user"]
    import_id = int(body["actions"][0]["value"])
    if not admins.is_admin(admin["id"]):
        return
    pending = db.fetch_one(
        "SELECT * FROM csv_imports WHERE id=%s AND status='pending'", (import_id,))
    if pending is None:
        notifications.update_import_admin_messages(
            client, {"id": import_id, "target_server_id": 0, "table_name": "?",
                     "requester_slack_id": "?", "database_name": "?"},
            ":information_source: This import was already decided.")
        return
    # Scope check: a CSV import creates/loads a table — a DDL-tier
    # operation. A scoped admin below DDL, or outside this target's scope,
    # must not approve it.
    if not _admin_in_scope(
            admin["id"], tier="ddl",
            target_server_id=pending["target_server_id"],
            requester_slack_id=pending["requester_slack_id"]):
        notifications.dm_requester(
            client, admin["id"],
            f":no_entry: CSV import `#{import_id}` needs DDL on a target "
            "outside your admin scope. Ask an admin with broader scope.")
        return
    with db.transaction() as cur:
        cur.execute(
            "UPDATE csv_imports SET status='approved', decided_by_slack_id=%s, "
            "decided_by_name=%s, decided_at=NOW() WHERE id=%s",
            (admin["id"], admin.get("name"), import_id))
        audit.log_in(cur, None, admin["id"], admin.get("name"), "import_approved",
                     {"import_id": import_id})
    imp = db.fetch_one("SELECT * FROM csv_imports WHERE id=%s", (import_id,))
    notifications.update_import_admin_messages(
        client, imp, f":hourglass: Approved by <@{admin['id']}> — importing now…")
    executor.submit_import(imp, client)


def handle_import_reject(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    admin = body["user"]
    import_id = int(body["actions"][0]["value"])
    if not admins.is_admin(admin["id"]):
        return
    pending = db.fetch_one(
        "SELECT * FROM csv_imports WHERE id=%s AND status='pending'", (import_id,))
    if pending is None:
        return
    with db.transaction() as cur:
        cur.execute(
            "UPDATE csv_imports SET status='rejected', decided_by_slack_id=%s, "
            "decided_by_name=%s, decided_at=NOW() WHERE id=%s",
            (admin["id"], admin.get("name"), import_id))
        audit.log_in(cur, None, admin["id"], admin.get("name"), "import_rejected",
                     {"import_id": import_id})
    imp = db.fetch_one("SELECT * FROM csv_imports WHERE id=%s", (import_id,))
    notifications.update_import_admin_messages(
        client, imp, f":no_entry: Rejected by <@{admin['id']}>.")
    notifications.dm_requester(
        client, imp["requester_slack_id"],
        f":no_entry: *CSV import `#{import_id}` rejected* by <@{admin['id']}>.")
    # Drop the uploaded CSV — rejected imports keep nothing.
    if imp.get("csv_file_path"):
        try:
            from pathlib import Path as _P
            _P(imp["csv_file_path"]).unlink(missing_ok=True)
        except OSError:
            pass


# ===========================================================================
# Edit & resubmit — reopen a pre-filled modal from a failed/rejected/
# changes-requested request so the user doesn't retype everything.
# ===========================================================================


def handle_resubmit(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    uid = body.get("user", {}).get("id")
    try:
        rid = int(body["actions"][0]["value"])
    except (KeyError, IndexError, ValueError, TypeError):
        return
    req = db.fetch_one(
        "SELECT id, requester_slack_id, target_server_id, database_name, query, "
        "       justification, result_format, wants_result, status "
        "FROM requests WHERE id = %s", (rid,))
    if req is None:
        return
    # Only the original requester may resubmit their own request.
    if req["requester_slack_id"] != uid:
        log.info("resubmit: %s tried to resubmit request %s owned by %s",
                 uid, rid, req["requester_slack_id"])
        return
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        return

    target = targets.get(req["target_server_id"])
    view = modal.build_modal(target_id=req["target_server_id"], principal_id=uid)
    # Pin target/db/query in private_metadata — external_select initial
    # values don't reach state.values unless touched (same pattern as
    # template loading); parse_submission falls back to these.
    pm: dict = {
        "target_id": req["target_server_id"],
        "database": req["database_name"],
        "query": req["query"],
    }
    # Editing a STILL-PENDING request: mark the modal so that submitting
    # the edited version atomically supersedes (withdraws) the original.
    # The original stays pending until then — closing the modal loses
    # nothing. Terminal sources (rejected / failed / changes_requested)
    # just prefill; there is nothing to supersede.
    if req["status"] == "pending":
        pm["supersedes"] = req["id"]
    # merge_pm rather than json.dumps, for the same reason every other writer
    # uses it: a later rebuild must not drop keys it does not know about.
    view["private_metadata"] = modal.merge_pm(None, **pm)
    # Opening an edit-and-resubmit modal is opening a query screen, so it gets
    # its own reserved id. The request being edited keeps its own number; this
    # one belongs to the new submission that will supersede it.
    _with_req_id(view, _reserve_quietly(uid))
    _inject_single_initials(view, {
        "target_server_id": req["target_server_id"],
        "target_alias": target.alias if target else None,
        "database_name": req["database_name"],
        "query": req["query"],
        "result_format": req.get("result_format"),
        "wants_result": req.get("wants_result"),
    }, prev_view={})
    # Justification isn't carried by _inject (it reads the batch view);
    # stamp it from the original request here.
    if req.get("justification"):
        for blk in view["blocks"]:
            if blk.get("block_id") == modal.B_JUSTIFICATION:
                blk["element"]["initial_value"] = req["justification"]
                break
    try:
        client.views_open(trigger_id=trigger_id, view=view)
    except Exception:
        log.exception("resubmit: views_open failed for request %s", rid)


# --- RO-burst nudge: 1-hour RO auto-approve window request -----------------

def _ro_window_update_admin_msg(client: WebClient, body: dict, text: str) -> None:
    """Replace the clicked admin DM's buttons with a plain status line."""
    try:
        ch = body["container"]["channel_id"]
        ts = body["container"]["message_ts"]
        notifications._update(
            client, channel=ch, ts=ts, text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
    except Exception:
        log.exception("ro_window: admin message update failed")


def _ro_window_already_decided(client: WebClient, body: dict, req: dict | None) -> None:
    status = req["status"] if req else "no longer available"
    _ro_window_update_admin_msg(
        client, body,
        f":information_source: This window request is already *{status}*.")


def handle_open_ro_window(ack: Ack, body: dict, client: WebClient) -> None:
    """Banner/CTA button → open the RO-window request modal (user picks the
    target + window there). The button value may carry a preselect target
    (from a read burst) or be empty (the always-available CTA)."""
    ack()
    user_id = body["user"]["id"]
    try:
        val = json.loads(body["actions"][0].get("value") or "{}")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        val = {}
    try:
        preselect = int(val["t"])
    except (KeyError, ValueError, TypeError):
        preselect = None
    user_targets = teams.list_targets_for_user(user_id)
    log.info("ro_window open: user=%s preselect=%s targets=%d",
             user_id, preselect, len(user_targets))
    # The banner/CTA button lives INSIDE the open /sql modal, so we PUSH a
    # new view onto that modal's stack. views_open (opening a new root modal)
    # returns ok=true from a modal context but Slack renders nothing — the
    # cause of the "click does nothing" report.
    if not user_targets:
        try:
            client.views_push(trigger_id=body["trigger_id"], view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Request RO window"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                    "text": "You don't have access to any targets yet, so there's "
                            "nothing to request a window for. Ask the DBA team."}}],
            })
        except Exception:
            log.exception("ro_window: views_push (no-targets) failed")
        return
    try:
        resp = client.views_push(
            trigger_id=body["trigger_id"],
            view=ro_window.request_modal(
                user_targets=user_targets,
                default_minutes=cfg.get_int("ro_window_minutes", 60),
                preselect_target_id=preselect,
            ),
        )
        log.info("ro_window: views_push ok=%s", resp.get("ok"))
    except Exception:
        log.exception("ro_window: views_push failed")


def handle_ro_window_submission(ack: Ack, body: dict, client: WebClient) -> None:
    """Justification modal submitted → persist a pending request + DM admins."""
    user = body["user"]
    if _kill_switch_on():
        ack({"response_action": "errors",
             "errors": {ro_window.B_REASON: _kill_switch_message()}})
        return
    state = body["view"]["state"]["values"]
    tsel = (state.get(ro_window.B_TARGET, {}).get(ro_window.A_TARGET, {}) or {}).get("selected_option")
    wsel = (state.get(ro_window.B_WINDOW, {}).get(ro_window.A_WINDOW, {}) or {}).get("selected_option")
    try:
        target_id = int(tsel["value"])
        window_minutes = int(wsel["value"])
    except (KeyError, ValueError, TypeError):
        ack({"response_action": "errors",
             "errors": {ro_window.B_TARGET: "Pick a target and a window length."}})
        return
    if not ro_window.is_valid_window(window_minutes):
        ack({"response_action": "errors",
             "errors": {ro_window.B_WINDOW: "Pick a valid window length."}})
        return
    reason = (state.get(ro_window.B_REASON, {})
              .get(ro_window.A_REASON, {})
              .get("value") or "").strip()
    if len(reason) < 5:
        ack({"response_action": "errors",
             "errors": {ro_window.B_REASON: "Please give a meaningful reason (at least 5 characters)."}})
        return
    # Authorization: re-check the picked target against the user's grants.
    # The picker only lists their targets, but a forged submission must not
    # be able to request a window on a target they can't reach.
    if not teams.can_use_target(user["id"], target_id):
        ack({"response_action": "errors",
             "errors": {ro_window.B_TARGET: "You don't have access to that target."}})
        return
    if auto_approve_requests.find_pending_for(user["id"], target_id) is not None:
        ack({"response_action": "errors",
             "errors": {ro_window.B_TARGET: "You already have a pending window request for this target."}})
        return
    t = targets.get(target_id)
    if t is None:
        ack({"response_action": "errors",
             "errors": {ro_window.B_TARGET: "That target no longer exists."}})
        return
    database_name = None  # target-scoped window (all DBs the user can read there)
    ack()
    row = auto_approve_requests.create(
        principal_id=user["id"],
        name=user.get("name") or user.get("username"),
        target_server_id=target_id,
        database_name=database_name,
        max_tier="ro",
        window_minutes=window_minutes,
        reason=reason,
    )
    if row is None:
        notifications.dm_requester(
            client, user["id"],
            ":warning: A pending window request for this target already exists.")
        return
    active = admins.list_active()
    if not active:
        notifications.dm_requester(
            client, user["id"],
            f":warning: Window request #{row['id']} saved, but no admins are "
            "configured to review it. Contact the DBA team.")
        return
    blocks = ro_window.admin_dm_blocks(row, t.alias)
    fallback = (f"RO auto-approve window request from <@{user['id']}> for "
                f"{t.alias} (#{row['id']})")
    for a in active:
        try:
            opened = client.conversations_open(users=a["slack_user_id"])
            notifications._post(client, channel=opened["channel"]["id"],
                                text=fallback, blocks=blocks)
        except Exception:
            log.exception("ro_window: admin DM failed for %s", a["slack_user_id"])
    notifications.dm_requester(
        client, user["id"],
        f":hourglass_flowing_sand: Window request *#{row['id']}* sent to the DBA "
        "team for review.")


def handle_ro_window_approve(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    actor = body["user"]
    if not admins.is_admin(actor["id"]):
        return
    try:
        rid = int(body["actions"][0]["value"])
    except (KeyError, IndexError, ValueError, TypeError):
        return
    req = auto_approve_requests.get(rid)
    if req is None or req["status"] != "pending":
        _ro_window_already_decided(client, body, req)
        return
    # Scope check: an RO-window grant hands out auto-approved access
    # on a target, so hold a scoped admin to their target + team scope (tier
    # is always RO here, within any max_tier).
    if not _admin_in_scope(
            actor["id"], tier=req["max_tier"],
            target_server_id=req["target_server_id"],
            requester_slack_id=req["requester_slack_id"]):
        notifications.dm_requester(
            client, actor["id"],
            f":no_entry: Window request `#{rid}` is on a target outside your "
            "admin scope. Ask an admin with broader scope to handle it.")
        return
    actor_name = actor.get("name") or actor.get("username")
    # Create the target-scoped grant + decide the request + audit, atomically.
    with db.connection() as conn:
        with conn.cursor() as cur:
            # This flow DMs the requester below — suppress the auth-event
            # outbox so the trigger doesn't double-DM.
            cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
            cur.execute(
                "INSERT INTO auto_approve_grants "
                "  (slack_user_id, max_tier, target_server_id, database_name, "
                "   expires_at, reason, granted_by) "
                "VALUES (%s, %s, %s, %s, NOW() + make_interval(mins => %s), %s, %s) "
                "RETURNING id",
                (req["requester_slack_id"], req["max_tier"], req["target_server_id"],
                 req["database_name"], req["window_minutes"],
                 f"window request #{rid}: {req['reason']}", actor["id"]),
            )
            grant_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE auto_approve_requests SET status='approved', "
                "  decided_by_slack_id=%s, decided_by_name=%s, granted_id=%s, "
                "  decided_at=NOW() WHERE id=%s AND status='pending'",
                (actor["id"], actor_name, grant_id, rid),
            )
            if cur.rowcount == 0:           # lost the race to another admin
                conn.rollback()
                _ro_window_already_decided(client, body, auto_approve_requests.get(rid))
                return
            cur.execute(
                "INSERT INTO audit_log (actor_slack_id, actor_name, action, details) "
                "VALUES (%s, %s, 'auto_approve_window_approved', %s::jsonb)",
                (actor["id"], actor_name, json.dumps({
                    "request_id": rid, "grant_id": grant_id,
                    "user": req["requester_slack_id"],
                    "target_server_id": req["target_server_id"],
                    "database_name": req["database_name"],
                    "max_tier": req["max_tier"], "window_minutes": req["window_minutes"]})),
            )
        conn.commit()
    t = targets.get(req["target_server_id"])
    alias = t.alias if t else f"target #{req['target_server_id']}"
    _ro_window_update_admin_msg(
        client, body,
        f":white_check_mark: *Window #{rid} approved* by <@{actor['id']}> — "
        f"{req['max_tier'].upper()} on `{alias}` for {req['window_minutes']} min.")
    notifications.dm_requester(
        client, req["requester_slack_id"],
        f":zap: Your *{req['max_tier'].upper()}* auto-approve window on `{alias}` is "
        f"active for the next {req['window_minutes']} min — matching queries dispatch "
        "immediately, no approval needed.")


def handle_ro_window_reject(ack: Ack, body: dict, client: WebClient) -> None:
    ack()
    actor = body["user"]
    if not admins.is_admin(actor["id"]):
        return
    try:
        rid = int(body["actions"][0]["value"])
    except (KeyError, IndexError, ValueError, TypeError):
        return
    decided = auto_approve_requests.decide(
        rid, status="rejected", decided_by_slack_id=actor["id"],
        decided_by_name=actor.get("name") or actor.get("username"))
    if decided is None:
        _ro_window_already_decided(client, body, auto_approve_requests.get(rid))
        return
    db.execute(
        "INSERT INTO audit_log (actor_slack_id, actor_name, action, details) "
        "VALUES (%s, %s, 'auto_approve_window_rejected', %s::jsonb)",
        (actor["id"], actor.get("name") or actor.get("username"),
         json.dumps({"request_id": rid, "user": decided["requester_slack_id"]})),
    )
    _ro_window_update_admin_msg(
        client, body, f":no_entry: *Window #{rid} rejected* by <@{actor['id']}>.")
    notifications.dm_requester(
        client, decided["requester_slack_id"],
        f":no_entry: Your auto-approve window request (#{rid}) was rejected.")


# ---------- schema browser (pushed on top of the /sql modal) ----------


def _schema_browser_scope(body: dict) -> tuple[int, str] | None:
    """(target_id, database) out of the browser view's private_metadata."""
    try:
        pm = json.loads(body["view"].get("private_metadata") or "{}")
        return int(pm["t"]), str(pm["d"])
    except (KeyError, ValueError, TypeError):
        return None


def _user_can_browse(user_id: str, target_id: int) -> bool:
    """Schema visibility follows query grants: admins everything, everyone
    else the targets they hold a grant on."""
    return admins.is_admin(user_id) or teams.can_use_target(user_id, target_id)


def handle_open_schema_browser(ack: Ack, body: dict, client: WebClient) -> None:
    """`Browse schema` button in the /sql modal → push the browser view for
    the currently selected target/database. Same views_push rule as the
    RO-window flow: views_open from inside a modal renders nothing."""
    ack()
    user_id = body["user"]["id"]
    view = body.get("view") or {}
    target_id = _target_id_from_view(view)
    values = view.get("state", {}).get("values", {})
    db_section = values.get(modal.B_DATABASE, {})
    db_block = next(
        (v for k, v in db_section.items() if k.startswith(modal.A_DATABASE)), {})
    database = (db_block.get("selected_option") or {}).get("value")

    def _push(pushed_view: dict) -> None:
        try:
            client.views_push(trigger_id=body["trigger_id"], view=pushed_view)
        except Exception:
            log.exception("schema_browser: views_push failed")

    if target_id is None or not database:
        _push(schema_browser.info_modal(
            ":point_up: Pick a *target* and a *database* in the form first — "
            "the schema browser shows the tables of that selection."))
        return
    if not _user_can_browse(user_id, target_id):
        _push(schema_browser.info_modal(
            ":no_entry: You don't have access to this target."))
        return
    target = targets.get(target_id)
    snapshot_ts = schema_catalog.snapshot_info(target_id, database)
    log.info("schema_browser open: user=%s target=%s db=%s snapshot=%s",
             user_id, target_id, database, snapshot_ts)
    if snapshot_ts is None:
        available = schema_catalog.list_snapshot_databases(target_id)
        hint = (" Snapshotted databases here: "
                + ", ".join(f"`{d}`" for d in available)) if available else (
                " The hourly catalog job hasn't covered this target yet.")
        _push(schema_browser.info_modal(
            f":hourglass: No schema snapshot for `{target.alias}/{database}`."
            + hint))
        return
    _push(schema_browser.browser_modal(
        target_id=target_id,
        target_alias=target.alias,
        database=database,
        snapshot_ts=snapshot_ts,
    ))


def handle_schema_table_options(ack: Ack, payload: dict, body: dict) -> None:
    """Typeahead for the browser's table picker, fed from the bot-DB
    snapshot (no target round-trip, so it fits the ack deadline)."""
    scope = _schema_browser_scope(body)
    user_id = body.get("user", {}).get("id") or ""
    if scope is None or not _user_can_browse(user_id, scope[0]):
        ack({"options": []})
        return
    typed = (payload.get("value") or "").strip()
    rows = schema_catalog.search_tables(scope[0], scope[1], typed, limit=50)
    ack({"options": [schema_browser.table_option(r) for r in rows]})


def handle_schema_table_selected(ack: Ack, body: dict, client: WebClient) -> None:
    """Table picked in the browser → re-render the same pushed view with
    the column/index detail below the picker."""
    ack()
    scope = _schema_browser_scope(body)
    user_id = body["user"]["id"]
    if scope is None or not _user_can_browse(user_id, scope[0]):
        return
    target_id, database = scope
    sel = body.get("actions", [{}])[0].get("selected_option") or {}
    table_ref = sel.get("value")
    if not table_ref:
        return
    res = schema_catalog.get_table(target_id, database, table_ref)
    if res is None or isinstance(res, list):
        # Vanished between snapshot refreshes (or ambiguous, which a
        # schema-qualified option value should preclude). Just re-render
        # the empty browser.
        body_blocks = [{"type": "section", "text": {
            "type": "mrkdwn",
            "text": f":mag: `{table_ref}` is not in the current snapshot."}}]
    else:
        trow, cols = res
        body_blocks = schema_browser.detail_blocks(trow, cols)
    target = targets.get(target_id)
    try:
        client.views_update(
            view_id=body["view"]["id"],
            hash=body["view"]["hash"],
            view=schema_browser.browser_modal(
                target_id=target_id,
                target_alias=target.alias if target else "?",
                database=database,
                snapshot_ts=schema_catalog.snapshot_info(target_id, database),
                selected_table=table_ref,
                body_blocks=body_blocks,
            ),
        )
    except Exception:
        log.exception("schema_browser: views_update failed")


# ---------- admin grant / revoke (/sql grant, /sql revoke) ----------


def _slack_profile(client: WebClient, user_id: str) -> dict:
    """Best-effort name / email / tz for a grantee, so a freshly
    whitelisted user gets a complete requesters row."""
    try:
        u = client.users_info(user=user_id)["user"]
        p = u.get("profile", {})
        return {"name": u.get("real_name") or p.get("real_name"),
                "email": p.get("email"), "tz": u.get("tz")}
    except Exception:
        log.exception("users_info failed for grantee %s", user_id)
        return {"name": None, "email": None, "tz": None}


def _granter_scope_target_ids(user_id: str) -> set | None:
    """Explicit target scope of a granting admin, or None for wildcard
    (super-admin, or a scoped admin whose scope_target_ids is NULL)."""
    row = db.fetch_one(
        "SELECT scope_target_ids FROM admins WHERE slack_user_id = %s", (user_id,))
    if row and row["scope_target_ids"]:
        return set(row["scope_target_ids"])
    return None


def handle_grant_target_options(ack: Ack, payload: dict, body: dict) -> None:
    """Target picker for the grant modal: enabled targets the granting
    admin may grant on (super-admin = all; scoped = their scope_target_ids),
    minus the bot's own control-plane DB."""
    user_id = body.get("user", {}).get("id") or ""
    if grants.authz(user_id) is None:
        ack({"options": []})
        return
    typed = (payload.get("value") or "").strip().lower()
    scope_ids = _granter_scope_target_ids(user_id)
    control_plane = grants.control_plane_target_ids()
    opts = []
    for t in targets.list_enabled():
        if t.id in control_plane:
            continue
        if scope_ids is not None and t.id not in scope_ids:
            continue
        if typed and typed not in t.alias.lower():
            continue
        opts.append({"text": {"type": "plain_text",
                              "text": targets.label_with_provider(t.alias, t.host)[:75]},
                     "value": str(t.id)})
        if len(opts) >= 100:
            break
    ack({"options": opts})


def handle_grant_db_options(ack: Ack, payload: dict, body: dict) -> None:
    """Database picker for the grant modal: the union of snapshotted
    databases across the picked target(s). Target ids come from
    private_metadata (written by handle_grant_target_changed), NOT the live
    state — Slack doesn't reliably include another input's selection in a
    block_suggestion payload (the cascading-select quirk). Empty target
    selection → no options yet (pick a target first)."""
    user_id = body.get("user", {}).get("id") or ""
    if grants.authz(user_id) is None:
        ack({"options": []})
        return
    try:
        pm = json.loads((body.get("view") or {}).get("private_metadata") or "{}")
        target_ids = pm.get("targets", []) if isinstance(pm, dict) else []
    except (ValueError, TypeError):
        target_ids = []
    typed = (payload.get("value") or "").strip().lower()
    dbs: set = set()
    for tid in target_ids:
        dbs.update(schema_catalog.list_snapshot_databases(tid))
    opts = [
        {"text": {"type": "plain_text", "text": d[:75]}, "value": d[:75]}
        for d in sorted(dbs) if not typed or typed in d.lower()
    ][:100]
    ack({"options": opts})


def handle_grant_target_changed(ack: Ack, body: dict, client: WebClient) -> None:
    """Target multi-select changed → store the picked target ids in the
    modal's private_metadata (so the DB picker can read them reliably) and
    re-render the view, preserving the user's other inputs. On a
    block_actions event the view state IS complete, so we read grantee /
    tier / reason from it to carry them through the rebuild."""
    ack()
    user = body["user"]
    cap = grants.authz(user["id"])
    if cap is None:
        return
    sel = body["actions"][0].get("selected_options") or []
    target_opts = [
        {"text": {"type": "plain_text", "text": o["text"]["text"][:75]},
         "value": o["value"]}
        for o in sel
    ]
    target_ids: list[int] = []
    for o in sel:
        try:
            target_ids.append(int(o["value"]))
        except (KeyError, ValueError, TypeError):
            continue
    st = body.get("view", {}).get("state", {}).get("values", {})
    grantee = (st.get(admin_grant.B_USER, {}).get(admin_grant.A_USER, {})
               .get("selected_user"))
    tier = (st.get(admin_grant.B_TIER, {}).get(admin_grant.A_TIER, {})
            .get("selected_option") or {}).get("value")
    reason = (st.get(admin_grant.B_REASON, {}).get(admin_grant.A_REASON, {})
              .get("value"))
    try:
        client.views_update(
            view_id=body["view"]["id"], hash=body["view"]["hash"],
            view=admin_grant.grant_modal(
                allowed_tiers=grants.allowed_tiers(cap),
                grantee=grantee, target_initial_options=target_opts,
                tier=tier, reason=reason, target_ids=target_ids))
    except Exception:
        log.exception("grant: views_update after target change failed")


def handle_grant_submission(ack: Ack, body: dict, client: WebClient) -> None:
    """Validate + apply an access grant from the grant modal. Re-checks
    capability, tier ceiling, target scope and the control-plane block on
    the server side — the modal only *offers* safe choices, it doesn't
    enforce them."""
    user = body["user"]
    cap = grants.authz(user["id"])
    if cap is None:
        ack({"response_action": "errors",
             "errors": {admin_grant.B_TARGET:
                        "You're no longer allowed to grant access."}})
        return
    state = body["view"]["state"]["values"]
    grantee = (state.get(admin_grant.B_USER, {}).get(admin_grant.A_USER, {})
               .get("selected_user"))
    tsels = (state.get(admin_grant.B_TARGET, {}).get(admin_grant.A_TARGET, {})
             .get("selected_options")) or []
    tier = (state.get(admin_grant.B_TIER, {}).get(admin_grant.A_TIER, {})
            .get("selected_option") or {}).get("value")
    dbs_sel = (state.get(admin_grant.B_DBS, {}).get(admin_grant.A_DBS, {})
               .get("selected_options")) or []
    reason = (state.get(admin_grant.B_REASON, {}).get(admin_grant.A_REASON, {})
              .get("value"))

    target_ids: list[int] = []
    for o in tsels:
        try:
            target_ids.append(int(o["value"]))
        except (KeyError, ValueError, TypeError):
            continue

    errors: dict = {}
    if not grantee:
        errors[admin_grant.B_USER] = "Pick a user."
    if not target_ids:
        errors[admin_grant.B_TARGET] = "Pick at least one target."
    elif set(target_ids) & grants.control_plane_target_ids():
        errors[admin_grant.B_TARGET] = "The bot's own DB can't be granted here."
    else:
        scope_ids = _granter_scope_target_ids(user["id"])
        if scope_ids is not None and any(t not in scope_ids for t in target_ids):
            errors[admin_grant.B_TARGET] = "One or more targets are outside your scope."
    if tier not in grants.allowed_tiers(cap):
        errors[admin_grant.B_TIER] = "You can't grant that tier."
    if errors:
        ack({"response_action": "errors", "errors": errors})
        return

    ack()  # close the modal; the rest is async work
    profile = _slack_profile(client, grantee)
    dbs = [o["value"] for o in dbs_sel] or None
    whitelisted = False
    aliases: list[str] = []
    for tid in target_ids:
        res = grants.grant(
            granter_id=user["id"], granter_name=user.get("name"),
            grantee_id=grantee, grantee_profile=profile,
            target_id=tid, mode=tier, databases=dbs, reason=reason,
            notify=False)  # handler sends one combined grantee DM below
        whitelisted = whitelisted or res["whitelisted_now"]
        t = targets.get(tid)
        aliases.append(t.alias if t else str(tid))

    scope = ", ".join(dbs) if dbs else "all databases"
    tlist = ", ".join(f"`{a}`" for a in aliases)
    # Grantee gets the ONE standard notification (combined: all picked
    # targets in a single DM). Same helper every grant path uses.
    grants.notify_grantee(grantee, user["id"], aliases, tier, dbs, whitelisted)
    # Granter (the admin) gets their own confirmation.
    notifications.dm_requester(
        client, user["id"],
        f":white_check_mark: Granted *{tier.upper()}* on {tlist} ({scope}) "
        f"to <@{grantee}>"
        + (" — whitelisted" if whitelisted else "") + ".")


def handle_revoke_user_options(ack: Ack, payload: dict, body: dict) -> None:
    """Options for the revoke user picker: ONLY users who hold at least one
    active grant (the revocable set), never the whole Slack workspace."""
    if grants.authz(body.get("user", {}).get("id") or "") is None:
        ack({"options": []})
        return
    typed = (payload.get("value") or "").strip()
    rows = grants.list_granted_users(typed=typed, limit=100)
    ack({"options": [admin_grant.revoke_user_option(r) for r in rows]})


def handle_revoke_user_picked(ack: Ack, body: dict, client: WebClient) -> None:
    """User chosen in the revoke modal → re-render it with that user's
    active per-user grants, each with a Revoke button."""
    ack()
    if grants.authz(body["user"]["id"]) is None:
        return
    sel = body["actions"][0].get("selected_option") or {}
    selected = sel.get("value")
    label = (sel.get("text") or {}).get("text")
    if not selected:
        return
    active = grants.list_active_grants(selected)
    try:
        client.views_update(
            view_id=body["view"]["id"], hash=body["view"]["hash"],
            view=admin_grant.revoke_modal(selected_user=selected,
                                          selected_label=label, grants=active))
    except Exception:
        log.exception("revoke: views_update after user pick failed")


def handle_revoke_click(ack: Ack, body: dict, client: WebClient) -> None:
    """Revoke button on one grant → revoke + DM the user + refresh the list."""
    ack()
    actor = body["user"]
    if grants.authz(actor["id"]) is None:
        return
    val = body["actions"][0].get("value") or ""
    try:
        grantee, tid = val.split(":", 1)
        target_id = int(tid)
    except (ValueError, TypeError):
        return
    # grants.revoke() notifies the grantee itself (default notify=True),
    # so no inline DM here — same helper every revoke path uses.
    grants.revoke(granter_id=actor["id"], granter_name=actor.get("name"),
                  grantee_id=grantee, target_id=target_id)
    try:
        client.views_update(
            view_id=body["view"]["id"], hash=body["view"]["hash"],
            view=admin_grant.revoke_modal(
                selected_user=grantee,
                grants=grants.list_active_grants(grantee)))
    except Exception:
        log.exception("revoke: views_update after revoke failed")
