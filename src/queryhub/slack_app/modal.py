"""Slack modal for /sql submissions."""
from __future__ import annotations

from .. import auto_approve, config as cfg, db, inventory, query_safety, targets, teams
from . import ro_window, schema_browser

MODAL_CALLBACK_ID = "sql_request_modal"
BATCH_MODAL_CALLBACK_ID = "sql_batch_modal"

# Per-item block/action prefixes for the batch modal. Each item gets a
# numeric suffix (e.g. blk_b_server_1, act_b_server_1). The database
# dropdown additionally carries the selected target_id as a `_v<tid>`
# salt so Slack's client treats the element as fresh after a target
# switch — same trick as the single-shot modal.
BATCH_B_SERVER       = "blk_b_server"        # + _<i>
BATCH_B_DATABASE     = "blk_b_database"      # + _<i>
BATCH_B_QUERY        = "blk_b_query"         # + _<i>
BATCH_B_WANTS_RESULT = "blk_b_wants_result"  # + _<i>
BATCH_B_JUSTIFICATION = "blk_b_justification"
BATCH_B_SCHEDULE_DATE = "blk_b_schedule_date"
BATCH_B_SCHEDULE_TIME = "blk_b_schedule_time"

BATCH_A_SERVER       = "act_b_server"        # + _<i>
BATCH_A_DATABASE     = "act_b_database"      # + _<i>_v<tid>
BATCH_A_QUERY        = "act_b_query"         # + _<i>
BATCH_A_WANTS_RESULT = "act_b_wants_result"  # + _<i>
BATCH_A_JUSTIFICATION = "act_b_justification"
BATCH_A_SCHEDULE_DATE = "act_b_schedule_date"
BATCH_A_SCHEDULE_TIME = "act_b_schedule_time"

BATCH_A_ADD_ITEM    = "act_b_add_item"
BATCH_A_REMOVE_ITEM = "act_b_remove_item"   # value carries the index to remove

# Mode toggle (single ↔ batch) — shown at the top of BOTH layouts so a
# user can flip without closing the modal.
B_MODE_TOGGLE = "blk_mode_toggle"
A_MODE_TOGGLE = "act_mode_toggle"
MODE_SINGLE = "single"
MODE_BATCH  = "batch"


def _mode_toggle_block(*, current_mode: str) -> dict:
    """Radio block at the top of the modal. `dispatch_action: true`
    fires handle_mode_toggle the moment the user changes the selection
    — no extra Submit needed."""
    single_opt = {
        "text": {"type": "plain_text", "text": "Single query"},
        "value": MODE_SINGLE,
    }
    batch_opt = {
        "text": {"type": "plain_text", "text": "Batch (up to 5)"},
        "value": MODE_BATCH,
    }
    initial = single_opt if current_mode == MODE_SINGLE else batch_opt
    return {
        "type": "input",
        "block_id": B_MODE_TOGGLE,
        "dispatch_action": True,
        "label": {"type": "plain_text", "text": "Submission type"},
        "element": {
            "type": "radio_buttons",
            "action_id": A_MODE_TOGGLE,
            "initial_option": initial,
            "options": [single_opt, batch_opt],
        },
    }


def _result_format_block(block_id: str, action_id: str,
                         default: str = "csv") -> dict:
    """Radio: No file / CSV / Excel. Single input element instead of
    the old (checkbox + format select) pair — fewer blocks, one
    decision. Values are the canonical strings persisted to
    requests.result_format ('csv', 'xlsx') plus 'none' for wants_result=False."""
    options = [
        {"text": {"type": "plain_text", "text": "Send me CSV"},
         "value": "csv"},
        {"text": {"type": "plain_text", "text": "Send me Excel (.xlsx)"},
         "value": "xlsx"},
        {"text": {"type": "plain_text", "text": "No file — execution status only"},
         "value": "none"},
    ]
    initial = next((o for o in options if o["value"] == default), options[0])
    return {
        "type": "input",
        "block_id": block_id,
        "optional": True,
        "label": {"type": "plain_text", "text": "Result"},
        "element": {
            "type": "radio_buttons",
            "action_id": action_id,
            "initial_option": initial,
            "options": options,
        },
    }


def _read_result_format(values: dict, block_id: str, action_id: str) -> tuple[bool, str]:
    """Pull (wants_result, result_format) out of view.state.values.
    `result_format` defaults to 'csv' so a legacy submission that
    doesn't carry the radio still ends up with a file. Returns
    wants_result=False iff the user explicitly picked 'No file'."""
    selected = (values.get(block_id, {})
                      .get(action_id, {})
                      .get("selected_option") or {})
    val = selected.get("value")
    if val == "none":
        return False, "csv"
    if val == "xlsx":
        return True, "xlsx"
    return True, "csv"


def read_mode_from_view(view: dict) -> str:
    """Pull the radio's current value out of view.state. Defaults to
    SINGLE so legacy callers that don't render the radio are safe."""
    values = (view or {}).get("state", {}).get("values", {})
    toggle = values.get(B_MODE_TOGGLE, {}).get(A_MODE_TOGGLE, {})
    selected = toggle.get("selected_option") or {}
    return selected.get("value") or MODE_SINGLE


def _optional_mode_toggle(current_mode: str) -> list[dict]:
    """Yield the mode-toggle block + divider only when the batch
    feature flag is on. Returns an empty list otherwise so the modal
    stays exactly as it was before batch shipped."""
    from .. import bundles as bundles_mod  # local import to avoid cycle
    if not bundles_mod.is_enabled():
        return []
    return [_mode_toggle_block(current_mode=current_mode), {"type": "divider"}]


def _recent_ro_burst(principal_id: str) -> dict | None:
    """Detect a read-heavy burst: >= `ro_burst_threshold` RO requests in the
    last `ro_burst_window_min` minutes. Returns {count, target_server_id,
    database_name} scoped to the target the user hit most in that window, or
    None. `requests` has no stored mode, so we classify the (few) recent
    queries with query_safety.required_mode()."""
    window_min = cfg.get_int("ro_burst_window_min", 10)
    threshold = cfg.get_int("ro_burst_threshold", 3)
    rows = db.fetch_all(
        "SELECT target_server_id, database_name, query FROM requests "
        " WHERE requester_slack_id = %s "
        "   AND created_at >= NOW() - make_interval(mins => %s) "
        " ORDER BY created_at DESC LIMIT 50",
        (principal_id, window_min),
    )
    ro = [r for r in rows
          if query_safety.required_mode(r["query"] or "") == "ro"]
    if len(ro) < threshold:
        return None
    from collections import Counter
    tally = Counter((r["target_server_id"], r["database_name"]) for r in ro)
    (tid, dbname), _ = tally.most_common(1)[0]
    return {"count": len(ro), "target_server_id": tid, "database_name": dbname}


def _auto_approve_banner(principal_id: str | None) -> list[dict]:
    """Top-of-modal banner: the active auto-approve badge (if any) plus the
    RO-burst nudge (Batch tip, and — when the user has no active grant — a
    1-hour RO-window request button). Always returns a list; never raises
    (the modal must open regardless)."""
    if not principal_id:
        return []
    blocks: list[dict] = []
    tier, expires_at, grant_id = auto_approve.best_active_tier(principal_id)
    has_grant = tier is not None
    if has_grant:
        until = auto_approve.fmt_until(expires_at)
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f":zap: *Auto-approve active* — up to *{tier.upper()}*, "
                    f"{until}. Queries at or below this tier dispatch "
                    "immediately on Submit (no admin approval needed). "
                    "Higher-tier queries still need admin approval."
                ),
            }],
        })
    try:
        burst = _recent_ro_burst(principal_id)
    except Exception:
        burst = None  # detection must never block the modal
    if burst:
        t = targets.get(burst["target_server_id"])
        alias = t.alias if t else f"target #{burst['target_server_id']}"
        blocks.extend(ro_window.nudge_blocks(
            count=burst["count"],
            window_min=cfg.get_int("ro_burst_window_min", 10),
            window_minutes=cfg.get_int("ro_window_minutes", 60),
            target_alias=alias,
            target_server_id=burst["target_server_id"],
            database_name=burst["database_name"],
            has_active_grant=has_grant,
        ))
    elif not has_grant:
        # No read-burst and no active grant: the window request is ALWAYS
        # available, just modest here (the burst branch above is the
        # prominent, 3rd-request-and-up version).
        blocks.extend(ro_window.request_cta_blocks())
    # Separate the banner group from the help tip / form below it.
    if blocks:
        blocks.append({"type": "divider"})
    return blocks

# block_id constants used to read values back on submission
B_SERVER = "blk_server"
B_DATABASE = "blk_database"
B_QUERY = "blk_query"
B_QUERY_FILE = "blk_query_file"
B_WANTS_RESULT = "blk_wants_result"
B_JUSTIFICATION = "blk_justification"
B_SCHEDULE_DATE = "blk_schedule_date"
B_SCHEDULE_TIME = "blk_schedule_time"

A_SERVER = "act_server"
A_DATABASE = "act_database"
A_QUERY = "act_query"
A_QUERY_FILE = "act_query_file"
A_WANTS_RESULT = "act_wants_result"
A_JUSTIFICATION = "act_justification"
A_SCHEDULE_DATE = "act_schedule_date"
A_SCHEDULE_TIME = "act_schedule_time"

B_SAVE_TEMPLATE = "blk_save_template"
A_SAVE_TEMPLATE = "act_save_template"
B_TEMPLATE_SHARE = "blk_template_share"
A_TEMPLATE_SHARE = "act_template_share"

# Load-from-template picker (external_select typeahead at the top
# of the modal). Action fires on select → views.update fills the
# target / database / query inputs from the chosen template.
B_LOAD_TEMPLATE = "blk_load_template"
A_LOAD_TEMPLATE = "act_load_template"

# Load-from-history picker — same mechanism as the template picker, but
# the options are the user's own last-N submitted requests.
B_LOAD_HISTORY = "blk_load_history"
A_LOAD_HISTORY = "act_load_history"

# Load-from-favorites picker — the user's starred queries.
B_LOAD_FAVORITE = "blk_load_favorite"
A_LOAD_FAVORITE = "act_load_favorite"

# "Favorite this query" checkbox at the bottom of the modal — stars the
# submitted query for the requester (read in the submit path).
B_SAVE_FAVORITE = "blk_save_favorite"
A_SAVE_FAVORITE = "act_save_favorite"


def options_for_targets(principal_id: str, query: str = "") -> list[dict]:
    """Build option list for `external_select`, filtered to the user's
    team-granted targets (admins see all). Slack limits to 100 options."""
    if query:
        matches = teams.search_targets_for_user(principal_id, query)
    else:
        matches = teams.list_targets_for_user(principal_id)
    return [
        {
            # Alias + cloud provider tag (AWS / Huawei), so the fleet is
            # legible at a glance during/after the cloud migration. Full
            # host still shows in the context block once selected.
            "text": {"type": "plain_text",
                     "text": (("[disabled] " if not t.enabled else "")
                              + targets.label_with_provider(t.alias, t.host))[:75]},
            "value": str(t.id),
        }
        for t in matches[:100]
    ]


def options_for_databases(
    principal_id: str,
    target_id: int | None,
    typed: str = "",
) -> list[dict]:
    """Database typeahead in the /sql modal. Pulls the candidate list from
    `inventory.v_all_databases` (single query, no per-target connect), then
    filters by the user's team grants and the typed prefix. Empty list if
    the user hasn't picked a target yet."""
    if not target_id:
        return []
    from .. import targets as targets_mod  # avoid circular import at module import time
    target = targets_mod.get(target_id)
    if target is None:
        return []
    candidates = inventory.list_databases_for_endpoint(target.host)
    if not candidates:
        # Inventory hasn't catalogued this endpoint (or it errored). At minimum
        # offer the target's recorded default_database so the user has SOMETHING
        # to pick — better than an empty dropdown.
        candidates = [target.default_database] if target.default_database else []
    # Constrain by team grants — but bypass users (see-everywhere) skip
    # the filter just like admins. Without this, a bypass user picking
    # a target their team has NO grant on gets an empty DB dropdown
    # (the function returns an empty allowed-set because of zero team
    # rows), even though they're allowed to query the whole target.
    if not teams._is_unrestricted(principal_id):
        allowed = teams.allowed_databases_for_user(principal_id, target_id)
        # allowed = None  → all DBs allowed (some grant is unrestricted)
        # allowed = set() → none allowed (shouldn't reach here; has_any_grant gates earlier)
        # allowed = {...} → narrow
        if allowed is not None:
            candidates = [d for d in candidates if d in allowed]
    typed_lower = typed.strip().lower()
    if typed_lower:
        candidates = [d for d in candidates if typed_lower in d.lower()]
    return [
        {
            "text": {"type": "plain_text", "text": d[:75]},
            "value": d,
        }
        for d in candidates[:100]
    ]


def _request_id_context(req_id: int | None) -> list[dict]:
    """`Request #N` above the form, or nothing when no id was reserved.

    Empty rather than a placeholder when reservation failed: a modal that opens
    is worth more than a number, so /sql never blocks on it.
    """
    if not req_id:
        return []
    return [{"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"Request `#{req_id}`"}]}]


def merge_pm(existing: str | dict | None, **updates) -> str:
    """Merge into the modal's private_metadata instead of replacing it.

    Three handlers write this field (template load, edit-and-resubmit, target
    selected) and each used to build a fresh dict, so a key it did not know
    about was dropped on the next rebuild. That is fine for `target_id`, which
    every writer sets — and wrong for the reserved request id, which is set once
    at open and has to survive every rebuild. Changing the target five times
    would otherwise reserve five ids and show a different number each time.
    """
    base: dict = {}
    if isinstance(existing, dict):
        base = dict(existing)
    elif existing:
        try:
            import json as _j
            loaded = _j.loads(existing)
            if isinstance(loaded, dict):
                base = loaded
        except (ValueError, TypeError):
            base = {}
    base.update({k: v for k, v in updates.items()})
    import json as _j
    return _j.dumps(base)


def build_modal(target_id: int | None = None,
                principal_id: str | None = None,
                req_id: int | None = None) -> dict:
    """Build the /sql modal. If `target_id` is provided, the database
    dropdown's action_id is salted with it (`act_database_v{target_id}`) so
    Slack's client treats the element as fresh and re-fires the options
    handler when the user re-opens the dropdown — without this, Slack caches
    the previous options and shows the wrong DB list after a target switch."""
    db_action_id = (
        f"{A_DATABASE}_v{target_id}" if target_id is not None else A_DATABASE
    )

    # When a target has been chosen, surface its full host:port as a
    # small context line under the dropdown — keeps the option text
    # clean (alias only) but still tells the user exactly which
    # endpoint they're pointing at.
    server_context_blocks: list[dict] = []
    if target_id is not None:
        from .. import targets as targets_mod
        target = targets_mod.get(target_id)
        if target is not None:
            # plain_text in context = small grey text, no auto-link.
            # mrkdwn would render host:port as a clickable URL — not what
            # we want here (it's informational, not actionable).
            server_context_blocks.append({
                "type": "context",
                "elements": [{
                    "type": "plain_text",
                    "text": f"{target.host}:{target.port}",
                    "emoji": False,
                }],
            })

    # Top hint about slash sub-commands — most users discover only the
    # modal, never the /sql history / whoami / help shortcuts. Single
    # context line keeps the hint cheap.
    top_hint = {
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                ":bulb: Tip: type `/sql help` for inline commands. "
                "Bookmark a query as a template (bottom of this form) "
                "to re-use it later."
            ),
        }],
    }

    # Load-from-template picker. external_select fires options
    # via handle_load_template_options (typeahead) and an action on
    # select via handle_load_template (modal views.update with the
    # picked template's target / db / query filled in). The picker
    # lives in the "Templates" group at the bottom of the modal —
    # putting it at the top confused users who hadn't saved one yet.
    load_template_block = {
        "type": "input",
        "block_id": B_LOAD_TEMPLATE,
        "optional": True,
        "dispatch_action": True,   # fire on select, not on submit
        "label": {"type": "plain_text",
                  "text": "Load from saved template (optional)"},
        "element": {
            "type": "external_select",
            "action_id": A_LOAD_TEMPLATE,
            "min_query_length": 0,
            "placeholder": {"type": "plain_text",
                            "text": "Pick a saved query to start from"},
        },
    }

    # Load-from-history picker — the user's own last-N requests. Same
    # views.update prefill mechanism as the template picker.
    load_history_block = {
        "type": "input",
        "block_id": B_LOAD_HISTORY,
        "optional": True,
        "dispatch_action": True,
        "label": {"type": "plain_text",
                  "text": "Load from recent history (optional)"},
        "element": {
            "type": "external_select",
            "action_id": A_LOAD_HISTORY,
            "min_query_length": 0,
            "placeholder": {"type": "plain_text",
                            "text": "Pick one of your last 10 queries"},
        },
    }

    # Load-from-favorites picker — the user's starred queries. Rendered at
    # the TOP of the modal for one-tap access to a starred query (picking one
    # prefills target / db / query via handle_load_favorite → views.update).
    load_favorite_block = {
        "type": "input",
        "block_id": B_LOAD_FAVORITE,
        "optional": True,
        "dispatch_action": True,
        "label": {"type": "plain_text",
                  "text": "⭐ Favorites — load a starred query"},
        "element": {
            "type": "external_select",
            "action_id": A_LOAD_FAVORITE,
            "min_query_length": 0,
            "placeholder": {"type": "plain_text",
                            "text": "Pick one of your starred queries"},
        },
    }

    # ⭐ Favorite-this-query checkbox — placed right under the query box so a
    # user never has to scroll to the bottom to star what they just wrote.
    save_favorite_block = {
        "type": "input",
        "block_id": B_SAVE_FAVORITE,
        "optional": True,
        "label": {"type": "plain_text", "text": "Favorite"},
        "element": {
            "type": "checkboxes",
            "action_id": A_SAVE_FAVORITE,
            "options": [{
                "text": {"type": "plain_text",
                         "text": "⭐ Add this query to my favorites"},
                "value": "favorite",
            }],
        },
    }

    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "New SQL Request"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            # The request id, reserved when this modal opened, so the number is
            # known before submitting — the same one the audit log will hold.
            # A context line rather than the title: the title is what Slack
            # truncates first on a narrow window.
            *_request_id_context(req_id),
            # Nudge banner first so the RO-window call-to-action sits above
            # the help tip and form (it gets lost otherwise).
            *_auto_approve_banner(principal_id),
            top_hint,
            # Favorites at the very top — one-tap access to a starred query.
            load_favorite_block,
            *_optional_mode_toggle(MODE_SINGLE),
            {
                "type": "input",
                "block_id": B_SERVER,
                # `dispatch_action: true` makes the external_select fire a
                # block_actions event the moment the user picks an option —
                # without this, Slack only sends values on modal submit, and
                # the cascading database dropdown wouldn't know which target
                # was chosen yet.
                "dispatch_action": True,
                "label": {"type": "plain_text", "text": "Target server"},
                "element": {
                    "type": "external_select",
                    "action_id": A_SERVER,
                    "placeholder": {"type": "plain_text", "text": "Type to search..."},
                    "min_query_length": 0,
                },
            },
            *server_context_blocks,
            {
                "type": "input",
                "block_id": B_DATABASE,
                "optional": True,
                "label": {"type": "plain_text", "text": "Database (leave blank for default)"},
                "element": {
                    "type": "external_select",
                    "action_id": db_action_id,
                    "min_query_length": 0,
                    "placeholder": {"type": "plain_text", "text": "Pick a database"},
                },
            },
            {
                "type": "input",
                "block_id": B_QUERY,
                "optional": True,
                "label": {"type": "plain_text", "text": "SQL query"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": A_QUERY,
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "SELECT ..."},
                },
            },
            {
                "type": "input",
                "block_id": B_QUERY_FILE,
                "optional": True,
                "label": {"type": "plain_text", "text": "...or upload a .sql file (instead of pasting)"},
                "element": {
                    "type": "file_input",
                    "action_id": A_QUERY_FILE,
                    "filetypes": ["sql", "txt"],
                    "max_files": 1,
                },
            },
            # ⭐ Favorite right under the query — no scrolling to the bottom.
            save_favorite_block,
            # 📖 Schema browser: look up a table's columns without losing
            # the draft (pushes a view on top; back arrow returns here).
            schema_browser.browse_cta_block(),
            {
                "type": "input",
                "block_id": B_JUSTIFICATION,
                "optional": True,
                "label": {"type": "plain_text", "text": "Justification (optional)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": A_JUSTIFICATION,
                    "placeholder": {"type": "plain_text", "text": "Why are you running this?"},
                },
            },
            _result_format_block(B_WANTS_RESULT, A_WANTS_RESULT, default="csv"),
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        ":alarm_clock: *Schedule (optional).* Leave both empty "
                        "to run as soon as approved. Pick a date *and* a time "
                        "— interpreted in your Slack profile timezone."
                    ),
                }],
            },
            {
                "type": "input",
                "block_id": B_SCHEDULE_DATE,
                "optional": True,
                "label": {"type": "plain_text", "text": "Run on"},
                "element": {
                    "type": "datepicker",
                    "action_id": A_SCHEDULE_DATE,
                    "placeholder": {"type": "plain_text", "text": "Pick a date"},
                },
            },
            {
                "type": "input",
                "block_id": B_SCHEDULE_TIME,
                "optional": True,
                "label": {"type": "plain_text", "text": "Run at"},
                "element": {
                    "type": "timepicker",
                    "action_id": A_SCHEDULE_TIME,
                    "placeholder": {"type": "plain_text", "text": "Pick a time"},
                },
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        ":arrows_counterclockwise: *Quick start.* Pre-fill this "
                        "form from a saved template or one of your recent "
                        "queries. All optional — leave blank to ignore. "
                        "(Favorites are at the top of the form.)"
                    ),
                }],
            },
            load_template_block,
            load_history_block,
            {"type": "divider"},
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        ":bookmark_tabs: *Save as template.* Name this query as a "
                        "reusable template to re-use later. Optional. "
                        "(To favorite it, use the ⭐ under the query box.)"
                    ),
                }],
            },
            {
                "type": "input",
                "block_id": B_SAVE_TEMPLATE,
                "optional": True,
                "label": {"type": "plain_text",
                          "text": "Save this query as a template (optional)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": A_SAVE_TEMPLATE,
                    "max_length": 64,
                    "placeholder": {"type": "plain_text",
                                    "text": "Template name — e.g. daily-active-users"},
                },
            },
            {
                "type": "input",
                "block_id": B_TEMPLATE_SHARE,
                "optional": True,
                "label": {"type": "plain_text", "text": "Share with team"},
                "element": {
                    "type": "checkboxes",
                    "action_id": A_TEMPLATE_SHARE,
                    "options": [{
                        "text": {"type": "plain_text",
                                 "text": "Make this template visible to everyone"},
                        "value": "shared",
                    }],
                },
            },
        ],
    }


def parse_submission(view_or_state: dict) -> dict:
    """Pull a clean dict out of the submitted modal state.

    Accepts either the full `view` dict (preferred — gives access to
    private_metadata too) or just the `view.state` payload (legacy
    shape kept for backward compatibility). The fallback to
    private_metadata covers the load-from-template flow: Slack's
    `initial_option` on an external_select is a visual pre-selection
    only — it does NOT appear in state.values unless the user
    explicitly interacts with the dropdown. So when the user picks a
    template and submits without re-touching the server picker,
    values[B_SERVER][A_SERVER]["selected_option"] is None. Falling
    back to private_metadata, where handle_target_selected (and the
    template loader, after this fix) records the chosen target_id,
    keeps submit working in that flow."""
    if "state" in view_or_state and "values" in view_or_state.get("state", {}):
        values = view_or_state["state"]["values"]
        pm_raw = view_or_state.get("private_metadata") or ""
    else:
        values = view_or_state.get("values", {})
        pm_raw = ""

    # Server: prefer the user-selected option; fall back to whatever
    # the template/cascade flow wrote into private_metadata.
    # The reserved request id, written into private_metadata when the modal
    # opened and merged through every rebuild. Absent for a modal opened before
    # this existed, or when the reservation failed — the submit then gets a
    # fresh id, which is the fail-open path core_submit._claim_draft expects.
    reserved_req_id = None
    if pm_raw:
        try:
            import json as _j
            _pm = _j.loads(pm_raw)
            if isinstance(_pm, dict):
                _rid = _pm.get("req_id")
                reserved_req_id = int(_rid) if _rid is not None else None
        except (ValueError, TypeError):
            reserved_req_id = None

    server_block = values.get(B_SERVER, {}).get(A_SERVER, {})
    selected_target = server_block.get("selected_option") or {}
    server_id_str = selected_target.get("value")
    if not server_id_str and pm_raw:
        try:
            import json as _j
            pm = _j.loads(pm_raw)
            tid = pm.get("target_id")
            if tid is not None:
                server_id_str = str(tid)
        except (ValueError, TypeError):
            pass
    if not server_id_str:
        raise ValueError("Submitted modal has no target server selected.")
    server_id = int(server_id_str)
    # Database action_id is dynamic (carries target_id suffix to bust the
    # Slack client's options cache when target changes). Scan for the first
    # action whose key starts with our static prefix.
    db_section = values.get(B_DATABASE, {})
    db_block = next(
        (v for k, v in db_section.items() if k.startswith(A_DATABASE)),
        {},
    )
    selected = db_block.get("selected_option") or {}
    database = selected.get("value") or None
    query = (values.get(B_QUERY, {}).get(A_QUERY, {}).get("value") or "").strip()
    # Pull the template-pinned fallbacks once if either of (database,
    # query) is empty. Same reason as the server fallback above:
    # plain_text_input + external_select `initial_*` values don't
    # reliably reach state.values after a views.update when the user
    # hasn't touched the element.
    if (not database or not query) and pm_raw:
        try:
            import json as _j
            pm = _j.loads(pm_raw)
            if not database:
                database = pm.get("database") or None
            if not query:
                query = (pm.get("query") or "").strip()
        except (ValueError, TypeError):
            pass
    # File input returns an array of file objects (we limit to one).
    file_block = values.get(B_QUERY_FILE, {}).get(A_QUERY_FILE, {})
    files = file_block.get("files") or []
    query_file_id = files[0]["id"] if files else None
    justification = (values[B_JUSTIFICATION][A_JUSTIFICATION].get("value") or "").strip() or None
    wants_result, result_format = _read_result_format(
        values, B_WANTS_RESULT, A_WANTS_RESULT,
    )
    sched_date = (
        values.get(B_SCHEDULE_DATE, {}).get(A_SCHEDULE_DATE, {}).get("selected_date")
    )
    sched_time = (
        values.get(B_SCHEDULE_TIME, {}).get(A_SCHEDULE_TIME, {}).get("selected_time")
    )
    template_name = (
        (values.get(B_SAVE_TEMPLATE, {})
               .get(A_SAVE_TEMPLATE, {})
               .get("value") or "").strip() or None
    )
    template_share = bool(
        values.get(B_TEMPLATE_SHARE, {})
              .get(A_TEMPLATE_SHARE, {})
              .get("selected_options")
    )
    favorite = bool(
        values.get(B_SAVE_FAVORITE, {})
              .get(A_SAVE_FAVORITE, {})
              .get("selected_options")
    )
    return {
        "req_id": reserved_req_id,
        "target_server_id": server_id,
        "database_name": database,
        "query": query,
        "query_file_id": query_file_id,
        "justification": justification,
        "wants_result": wants_result,
        "result_format": result_format,
        "schedule_date": sched_date,  # 'YYYY-MM-DD' or None
        "schedule_time": sched_time,  # 'HH:MM' or None
        "template_name": template_name,
        "template_share": template_share,
        "favorite": favorite,
    }


# ===========================================================================
# Batch modal — `/sql batch`
# ===========================================================================
#
# Up to N items (target / database / query / wants_result) plus a single
# bundle-level justification and optional scheduled_for. Add / Remove
# buttons mutate the view via `views.update`. Existing state on the view
# is preserved by passing it back as `initial_*` on each render.
#
# Per-item block_ids carry a 1-based suffix (`blk_b_server_1`, etc.).
# Database action_ids additionally carry the currently-selected target
# id as a salt (`act_b_database_1_v23`) so Slack's client refreshes the
# typeahead options after a target change — same trick the single-shot
# modal uses.


def _batch_item_blocks(
    *,
    index: int,
    selected_target_id: int | None,
    target_alias: str | None,
    target_host_port: str | None,
    selected_database: str | None,
    query_text: str,
    wants_result: bool,
    result_format: str | None,
    can_remove: bool,
) -> list[dict]:
    """Render one item's blocks. `selected_*` / `target_*` / `query_text`
    / `wants_result` carry the state that should survive a views.update."""
    db_action_id = (
        f"{BATCH_A_DATABASE}_{index}_v{selected_target_id}"
        if selected_target_id is not None
        else f"{BATCH_A_DATABASE}_{index}"
    )

    # Header / divider for this item.
    header = {
        "type": "header",
        "text": {"type": "plain_text", "text": f"Item #{index}"},
    }

    server_input: dict = {
        "type": "input",
        "block_id": f"{BATCH_B_SERVER}_{index}",
        "dispatch_action": True,  # fire on select for cascade
        "label": {"type": "plain_text", "text": "Target server"},
        "element": {
            "type": "external_select",
            "action_id": f"{BATCH_A_SERVER}_{index}",
            "placeholder": {"type": "plain_text", "text": "Type to search..."},
            "min_query_length": 0,
        },
    }
    if selected_target_id is not None and target_alias:
        server_input["element"]["initial_option"] = {
            "text": {"type": "plain_text", "text": target_alias[:75]},
            "value": str(selected_target_id),
        }

    server_context: list[dict] = []
    if target_host_port:
        server_context.append({
            "type": "context",
            "elements": [{
                "type": "plain_text",
                "text": target_host_port,
                "emoji": False,
            }],
        })

    database_input: dict = {
        "type": "input",
        "block_id": f"{BATCH_B_DATABASE}_{index}",
        "optional": True,
        "label": {"type": "plain_text", "text": "Database (leave blank for default)"},
        "element": {
            "type": "external_select",
            "action_id": db_action_id,
            "min_query_length": 0,
            "placeholder": {"type": "plain_text", "text": "Pick a database"},
        },
    }
    if selected_database:
        database_input["element"]["initial_option"] = {
            "text": {"type": "plain_text", "text": selected_database[:75]},
            "value": selected_database,
        }

    query_input: dict = {
        "type": "input",
        "block_id": f"{BATCH_B_QUERY}_{index}",
        "optional": True,  # validated server-side on submit
        "label": {"type": "plain_text", "text": "SQL query"},
        "element": {
            "type": "plain_text_input",
            "action_id": f"{BATCH_A_QUERY}_{index}",
            "multiline": True,
            "placeholder": {"type": "plain_text", "text": "SELECT ..."},
        },
    }
    if query_text:
        query_input["element"]["initial_value"] = query_text

    # Result-format radio. Per-item, preserved across views.update via
    # items_state.result_format. Pick the explicit format if the caller
    # passed one; otherwise fall back to the boolean wants_result
    # (True = 'csv' default, False = 'none', None = first render = 'csv').
    if result_format in ("csv", "xlsx", "none"):
        rf_default = result_format
    elif wants_result is False:
        rf_default = "none"
    else:
        rf_default = "csv"
    wants_input = _result_format_block(
        f"{BATCH_B_WANTS_RESULT}_{index}",
        f"{BATCH_A_WANTS_RESULT}_{index}",
        default=rf_default,
    )

    blocks: list[dict] = [header, server_input, *server_context,
                          database_input, query_input, wants_input]

    if can_remove:
        blocks.append({
            "type": "actions",
            "block_id": f"blk_b_remove_{index}",
            "elements": [{
                "type": "button",
                "action_id": BATCH_A_REMOVE_ITEM,
                "value": str(index),
                "text": {"type": "plain_text", "text": f"Remove item #{index}"},
                "style": "danger",
            }],
        })

    return blocks


def build_batch_modal(
    *,
    item_count: int,
    items_state: list[dict] | None = None,
    justification: str = "",
    schedule_date: str | None = None,
    schedule_time: str | None = None,
    max_items: int = 5,
    principal_id: str | None = None,
) -> dict:
    """Render the batch modal.

    `items_state` is a list (length == item_count) of dicts with the
    keys we preserve across views.update calls:
        target_server_id, target_alias, target_host_port,
        database_name, query, wants_result
    Any field missing or None is rendered empty.
    """
    if item_count < 1:
        item_count = 1
    if item_count > max_items:
        item_count = max_items
    items_state = items_state or []

    # Pad to current item_count so indexing is safe.
    while len(items_state) < item_count:
        items_state.append({})

    # Batch-wide default: users almost always run every item against the
    # same server + database. Any item that doesn't have its own target
    # selected yet (a freshly added row, or one the user never touched)
    # inherits the target + database of the first item that does. The guard
    # skips any item that already carries a target, so a per-item override
    # is never overwritten and changing item #1 later does not retro-rewrite
    # rows the user already filled. Result format / query stay per-item.
    default_item = next(
        (st for st in items_state[:item_count] if st.get("target_server_id")),
        None,
    )
    if default_item is not None:
        for st in items_state[:item_count]:
            if not st.get("target_server_id"):
                st["target_server_id"] = default_item.get("target_server_id")
                st["target_alias"] = default_item.get("target_alias")
                st["target_host_port"] = default_item.get("target_host_port")
                st["database_name"] = default_item.get("database_name")

    blocks: list[dict] = [
        *_auto_approve_banner(principal_id),
        *_optional_mode_toggle(MODE_BATCH),
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f":package: *Batch mode* — submit up to {max_items} "
                    "queries in one approval round. Justification and "
                    "scheduling apply to the whole batch."
                ),
            }],
        },
    ]

    for i in range(1, item_count + 1):
        st = items_state[i - 1]
        blocks.extend(_batch_item_blocks(
            index=i,
            selected_target_id=st.get("target_server_id"),
            target_alias=st.get("target_alias"),
            target_host_port=st.get("target_host_port"),
            selected_database=st.get("database_name"),
            query_text=st.get("query") or "",
            # Pass None when the items_state didn't carry a value so
            # _batch_item_blocks can apply the "default csv" rule on
            # first render. Pass True/False verbatim once the user has
            # made an explicit choice (preserved across views.update).
            wants_result=st.get("wants_result"),
            result_format=st.get("result_format"),
            # First item is never removable so the modal always has ≥1.
            can_remove=(item_count > 1),
        ))
        blocks.append({"type": "divider"})

    # "Add another" button — hidden once we've hit the cap.
    if item_count < max_items:
        blocks.append({
            "type": "actions",
            "block_id": "blk_b_add",
            "elements": [{
                "type": "button",
                "action_id": BATCH_A_ADD_ITEM,
                "text": {"type": "plain_text",
                         "text": f"+ Add another item ({item_count}/{max_items})"},
            }],
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (f":no_entry_sign: Maximum of {max_items} items reached. "
                         "Submit this batch first, then start another if needed."),
            }],
        })

    # Bundle-level fields.
    justification_input: dict = {
        "type": "input",
        "block_id": BATCH_B_JUSTIFICATION,
        "optional": True,
        "label": {"type": "plain_text",
                  "text": "Justification (applies to whole batch)"},
        "element": {
            "type": "plain_text_input",
            "action_id": BATCH_A_JUSTIFICATION,
            "multiline": True,
            "placeholder": {"type": "plain_text",
                            "text": "Why are these queries needed?"},
        },
    }
    if justification:
        justification_input["element"]["initial_value"] = justification
    blocks.append(justification_input)

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                ":alarm_clock: *Schedule (optional, whole-batch).* Leave "
                "both empty to run as soon as approved. Pick a date *and* "
                "a time — interpreted in your Slack profile timezone."
            ),
        }],
    })

    sched_date_input: dict = {
        "type": "input",
        "block_id": BATCH_B_SCHEDULE_DATE,
        "optional": True,
        "label": {"type": "plain_text", "text": "Run on"},
        "element": {
            "type": "datepicker",
            "action_id": BATCH_A_SCHEDULE_DATE,
            "placeholder": {"type": "plain_text", "text": "Pick a date"},
        },
    }
    if schedule_date:
        sched_date_input["element"]["initial_date"] = schedule_date
    blocks.append(sched_date_input)

    sched_time_input: dict = {
        "type": "input",
        "block_id": BATCH_B_SCHEDULE_TIME,
        "optional": True,
        "label": {"type": "plain_text", "text": "Run at"},
        "element": {
            "type": "timepicker",
            "action_id": BATCH_A_SCHEDULE_TIME,
            "placeholder": {"type": "plain_text", "text": "Pick a time"},
        },
    }
    if schedule_time:
        sched_time_input["element"]["initial_time"] = schedule_time
    blocks.append(sched_time_input)

    return {
        "type": "modal",
        "callback_id": BATCH_MODAL_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "New SQL Batch"},
        "submit": {"type": "plain_text", "text": "Submit batch"},
        "close": {"type": "plain_text", "text": "Cancel"},
        # Used by Add/Remove handlers to know the current item count
        # without re-walking the blocks. We also stash the per-item
        # target_id so the database typeahead salt survives re-render.
        "private_metadata": _encode_pm(item_count, items_state),
        "blocks": blocks,
    }


# --- Batch private_metadata helpers ---------------------------------------
#
# Slack caps private_metadata at 3000 bytes. We store ONLY the bits we
# can't reconstruct from view.state on the next round-trip:
#   - item_count: drives the next render
#   - per-item target_server_id: needed for the db dropdown action_id salt
#     and the host:port context line
#   - per-item target_alias / host_port: cached so we don't re-query the
#     DB on every views.update
#
# Query text and wants_result come straight from view.state.

import json as _json


def _encode_pm(item_count: int, items_state: list[dict]) -> str:
    payload = {
        "n": item_count,
        "items": [
            {
                "tid": st.get("target_server_id"),
                "ta":  st.get("target_alias"),
                "thp": st.get("target_host_port"),
                "db":  st.get("database_name"),
            }
            for st in items_state[:item_count]
        ],
    }
    return _json.dumps(payload, separators=(",", ":"))


def decode_pm(pm: str | None) -> dict:
    if not pm:
        return {"n": 1, "items": []}
    try:
        data = _json.loads(pm)
        return {
            "n": int(data.get("n", 1)),
            "items": list(data.get("items", [])),
        }
    except (ValueError, TypeError):
        return {"n": 1, "items": []}


def read_batch_state_from_view(view: dict) -> list[dict]:
    """Pull current per-item state out of view.state.values + private_metadata.

    Returns a list of dicts indexed 0..n-1, each with:
        target_server_id (int|None), target_alias (str|None),
        target_host_port (str|None), database_name (str|None),
        query (str), wants_result (bool)
    """
    pm = decode_pm(view.get("private_metadata"))
    n = pm["n"]
    pm_items = pm["items"]
    values = view.get("state", {}).get("values", {})

    items: list[dict] = []
    for idx in range(1, n + 1):
        # Target: external_select with action_id `act_b_server_<i>`.
        server_block = values.get(f"{BATCH_B_SERVER}_{idx}", {})
        server_action = server_block.get(f"{BATCH_A_SERVER}_{idx}", {})
        selected = server_action.get("selected_option")
        if selected and selected.get("value"):
            tid = int(selected["value"])
            # Strip any "[disabled] " prefix from the rendered alias.
            alias_text = (selected.get("text") or {}).get("text", "") or ""
            alias = alias_text.replace("[disabled] ", "", 1) or None
            target_host_port = None  # only known if we previously cached it
            # If pm has a cache for this index AND tid still matches, reuse host_port.
            if idx - 1 < len(pm_items) and pm_items[idx - 1].get("tid") == tid:
                target_host_port = pm_items[idx - 1].get("thp")
            if target_host_port is None:
                # Cheap DB lookup — only on items where the user just changed
                # the target. Acceptable cost in interactive flow.
                from .. import targets as targets_mod
                t = targets_mod.get(tid)
                if t is not None:
                    target_host_port = f"{t.host}:{t.port}"
                    if not alias:
                        alias = t.alias
        else:
            tid = None
            alias = None
            target_host_port = None
            # Fall back to whatever pm had (state can be empty right after
            # a views.update if the user hadn't touched the field).
            if idx - 1 < len(pm_items):
                cached = pm_items[idx - 1]
                tid = cached.get("tid")
                alias = cached.get("ta")
                target_host_port = cached.get("thp")

        # Database: dynamic action_id, scan by prefix.
        db_section = values.get(f"{BATCH_B_DATABASE}_{idx}", {})
        db_block = next(
            (v for k, v in db_section.items() if k.startswith(BATCH_A_DATABASE)),
            {},
        )
        db_selected = db_block.get("selected_option") or {}
        database = db_selected.get("value") or None
        if database is None and idx - 1 < len(pm_items):
            # Preserve pm cache if user hasn't changed it in this round.
            database = pm_items[idx - 1].get("db")

        # Query + wants_result.
        query_block = values.get(f"{BATCH_B_QUERY}_{idx}", {})
        query_text = (
            query_block.get(f"{BATCH_A_QUERY}_{idx}", {}).get("value") or ""
        ).strip()

        wants_result, result_format = _read_result_format(
            values,
            f"{BATCH_B_WANTS_RESULT}_{idx}",
            f"{BATCH_A_WANTS_RESULT}_{idx}",
        )

        items.append({
            "target_server_id": tid,
            "target_alias": alias,
            "target_host_port": target_host_port,
            "database_name": database,
            "query": query_text,
            "wants_result": wants_result,
            "result_format": result_format,
        })

    return items


def parse_batch_submission(view: dict) -> dict:
    """Final-submit parse. Returns a clean dict ready for validation +
    insert. The submit handler is responsible for per-item validation
    (mode check, pre-flight, duplicate guard, etc.)."""
    items = read_batch_state_from_view(view)
    values = view.get("state", {}).get("values", {})

    justification = (
        (values.get(BATCH_B_JUSTIFICATION, {})
               .get(BATCH_A_JUSTIFICATION, {})
               .get("value") or "").strip() or None
    )
    sched_date = (values.get(BATCH_B_SCHEDULE_DATE, {})
                        .get(BATCH_A_SCHEDULE_DATE, {})
                        .get("selected_date"))
    sched_time = (values.get(BATCH_B_SCHEDULE_TIME, {})
                        .get(BATCH_A_SCHEDULE_TIME, {})
                        .get("selected_time"))
    return {
        "items": items,
        "justification": justification,
        "schedule_date": sched_date,
        "schedule_time": sched_time,
    }



# ===========================================================================
# CSV import modal (`/sql import`)
# ===========================================================================

IMPORT_MODAL_CALLBACK_ID = "sql_import_modal"

B_IMPORT_SERVER     = "blk_import_server";     A_IMPORT_SERVER     = "act_import_server"
B_IMPORT_DATABASE   = "blk_import_database";   A_IMPORT_DATABASE   = "act_import_database"
B_IMPORT_FILE       = "blk_import_file";       A_IMPORT_FILE       = "act_import_file"
B_IMPORT_TABLE_MODE = "blk_import_table_mode"; A_IMPORT_TABLE_MODE = "act_import_table_mode"
B_IMPORT_TABLE_NAME = "blk_import_table_name"; A_IMPORT_TABLE_NAME = "act_import_table_name"
B_IMPORT_COLDEFS    = "blk_import_coldefs";    A_IMPORT_COLDEFS    = "act_import_coldefs"
B_IMPORT_DELIM      = "blk_import_delim";      A_IMPORT_DELIM      = "act_import_delim"
B_IMPORT_PERSIST    = "blk_import_persist";    A_IMPORT_PERSIST    = "act_import_persist"

IMPORT_MODE_NEW      = "new"
IMPORT_MODE_EXISTING = "existing"


def build_import_modal() -> dict:
    """The `/sql import` modal: target + database + CSV upload + target
    table (new/existing in the dba schema) + delimiter + persistence."""
    delim_opts = [
        {"text": {"type": "plain_text", "text": "Comma  ,"},      "value": "comma"},
        {"text": {"type": "plain_text", "text": "Semicolon  ;"},  "value": "semicolon"},
        {"text": {"type": "plain_text", "text": "Tab"},           "value": "tab"},
    ]
    mode_opts = [
        {"text": {"type": "plain_text", "text": "New table (auto-create in dba)"},
         "value": IMPORT_MODE_NEW},
        {"text": {"type": "plain_text", "text": "Existing dba.* table (append)"},
         "value": IMPORT_MODE_EXISTING},
    ]
    persist_opts = [
        {"text": {"type": "plain_text", "text": "Temporary — one-off / scratch"},
         "description": {"type": "plain_text",
             "text": "UNLOGGED: fastest load, but the data is WIPED if the DB "
                     "restarts/crashes and is NOT backed up or replicated."},
         "value": "temp"},
        {"text": {"type": "plain_text", "text": "Permanent — keep the data"},
         "description": {"type": "plain_text",
             "text": "LOGGED: a normal durable table — crash-safe, backed up, "
                     "replicated. Slightly slower load."},
         "value": "perm"},
    ]
    return {
        "type": "modal",
        "callback_id": IMPORT_MODAL_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "CSV Import"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "context", "elements": [{"type": "mrkdwn",
                "text": ":inbox_tray: Bulk-load a CSV into the *dba* schema. "
                        "Admin approval required."}]},
            {"type": "input", "block_id": B_IMPORT_SERVER,
             "label": {"type": "plain_text", "text": "Target server"},
             "element": {"type": "external_select", "action_id": A_IMPORT_SERVER,
                         "min_query_length": 0,
                         "placeholder": {"type": "plain_text", "text": "Pick a target"}}},
            {"type": "input", "block_id": B_IMPORT_DATABASE, "optional": True,
             "label": {"type": "plain_text", "text": "Database (blank = target default)"},
             "element": {"type": "plain_text_input", "action_id": A_IMPORT_DATABASE,
                         "placeholder": {"type": "plain_text", "text": "e.g. balance_service"}}},
            {"type": "input", "block_id": B_IMPORT_FILE,
             "label": {"type": "plain_text", "text": "CSV file (UTF-8, with header row)"},
             "element": {"type": "file_input", "action_id": A_IMPORT_FILE,
                         "filetypes": ["csv"], "max_files": 1}},
            {"type": "input", "block_id": B_IMPORT_TABLE_MODE,
             "label": {"type": "plain_text", "text": "Target table"},
             "element": {"type": "radio_buttons", "action_id": A_IMPORT_TABLE_MODE,
                         "initial_option": mode_opts[0], "options": mode_opts}},
            {"type": "input", "block_id": B_IMPORT_TABLE_NAME,
             "label": {"type": "plain_text", "text": "Table name (in dba schema)"},
             "element": {"type": "plain_text_input", "action_id": A_IMPORT_TABLE_NAME,
                         "placeholder": {"type": "plain_text", "text": "e.g. import_balances_2026"}}},
            {"type": "input", "block_id": B_IMPORT_COLDEFS, "optional": True,
             "label": {"type": "plain_text", "text": "Column types (optional, new table only)"},
             "element": {"type": "plain_text_input", "action_id": A_IMPORT_COLDEFS,
                         "multiline": True,
                         "placeholder": {"type": "plain_text",
                             "text": "Blank = all TEXT. e.g. id int, name text, amount numeric(10,2)"}},
             "hint": {"type": "plain_text",
                      "text": "Define types yourself instead of all-TEXT. Must list "
                              "every CSV column in order. Type mismatch fails the load."}},
            {"type": "input", "block_id": B_IMPORT_DELIM,
             "label": {"type": "plain_text", "text": "Column delimiter"},
             "element": {"type": "radio_buttons", "action_id": A_IMPORT_DELIM,
                         "initial_option": delim_opts[0], "options": delim_opts}},
            {"type": "input", "block_id": B_IMPORT_PERSIST,
             "label": {"type": "plain_text", "text": "How long do you need this data? (new table only)"},
             "element": {"type": "radio_buttons", "action_id": A_IMPORT_PERSIST,
                         "initial_option": persist_opts[0], "options": persist_opts}},
        ],
    }


def parse_import_submission(view: dict) -> dict:
    """Pull the import modal's fields out of the view state."""
    v = view["state"]["values"]
    server = v.get(B_IMPORT_SERVER, {}).get(A_IMPORT_SERVER, {}).get("selected_option")
    target_id = int(server["value"]) if server else None
    database = (v.get(B_IMPORT_DATABASE, {}).get(A_IMPORT_DATABASE, {}).get("value") or "").strip() or None
    files = v.get(B_IMPORT_FILE, {}).get(A_IMPORT_FILE, {}).get("files") or []
    file_id = files[0]["id"] if files else None
    mode_opt = v.get(B_IMPORT_TABLE_MODE, {}).get(A_IMPORT_TABLE_MODE, {}).get("selected_option") or {}
    table_mode = mode_opt.get("value", IMPORT_MODE_NEW)
    table_name = (v.get(B_IMPORT_TABLE_NAME, {}).get(A_IMPORT_TABLE_NAME, {}).get("value") or "").strip()
    coldefs_text = (v.get(B_IMPORT_COLDEFS, {}).get(A_IMPORT_COLDEFS, {}).get("value") or "").strip()
    delim_opt = v.get(B_IMPORT_DELIM, {}).get(A_IMPORT_DELIM, {}).get("selected_option") or {}
    delimiter_key = delim_opt.get("value", "comma")
    persist_opt = v.get(B_IMPORT_PERSIST, {}).get(A_IMPORT_PERSIST, {}).get("selected_option") or {}
    unlogged = persist_opt.get("value", "temp") == "temp"
    return {
        "target_server_id": target_id,
        "database": database,
        "file_id": file_id,
        "table_mode": table_mode,
        "table_name": table_name,
        "coldefs_text": coldefs_text,
        "delimiter_key": delimiter_key,
        "unlogged": unlogged,
    }
