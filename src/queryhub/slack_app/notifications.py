"""Send admin DMs and update them in lockstep when a decision is made."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

try:
    from slack_sdk.errors import SlackApiError
except ModuleNotFoundError:  # vanilla profile: the [slack] extra isn't installed
    class SlackApiError(Exception):  # type: ignore[no-redef]  # sentinel; these send paths don't run here
        pass

if TYPE_CHECKING:  # only a type hint — no runtime dependency on slack_sdk
    from slack_sdk.web import WebClient

from .. import admins, db, origins, query_safety, targets
from .. import config as cfg

log = logging.getLogger(__name__)

ACTION_APPROVE = "act_approve"
ACTION_REJECT = "act_reject"
ACTION_REQUEST_CHANGES = "act_request_changes"
ACTION_CANCEL_SCHEDULED = "act_cancel_scheduled"
ACTION_CANCEL_REQUEST = "act_cancel_request"  # requester withdraws own pending request
ACTION_DBA_MARK_COMPLETED = "act_dba_mark_completed"
ACTION_DBA_MARK_FAILED = "act_dba_mark_failed"

# Queries up to this length get an inline code block in the admin DM; longer
# ones are uploaded as a `.sql` thread snippet (Slack renders short blocks
# inline cleanly, but very long inline blocks are awkward to read/scroll).
INLINE_QUERY_MAX_CHARS = 500


# ---------- attachment colors (tier + status -> theme token hex) ----------
#
# Slack message attachments carry a `color` field that paints a 4-pixel
# bar down the left side of the card. We use it to give admins an
# instant tier / decision read at a glance — green for RO, amber for
# RW, red for DDL or anything destructive, gray for resolved-cancelled.

_COLOR_BRAND  = "#C4603F"   # brand accent — RO / completed
_COLOR_AMBER  = "#F59E0B"   # warning       — RW / awaiting / changes_requested
_COLOR_DANGER = "#E53D3D"   # danger        — DDL / destructive / failed / rejected
_COLOR_NEUTRAL = "#5A6170"  # system        — cancelled


def _tier_color(request: dict) -> str:
    """Initial color when a request is first posted to admins.
    Pure tier mapping (RO=green, RW=amber, DDL=red). RW already
    implies mutation — no need to bump those to red on top."""
    safety = query_safety.analyze(request.get("query") or "")
    tier = (safety.main_tier or "ro").lower()
    return {"ro": _COLOR_BRAND, "rw": _COLOR_AMBER,
            "ddl": _COLOR_DANGER}.get(tier, _COLOR_BRAND)


def _bundle_color(items: list[dict]) -> str:
    """Pick the most severe per-item color across a bundle's items —
    so a 5-item bundle with one DDL paints red, with one RW paints
    amber, and all-RO paints green. Status colors override
    tier on terminal bundles (decided / cancelled / partial)."""
    severity = {_COLOR_BRAND: 0, _COLOR_AMBER: 1,
                _COLOR_DANGER: 2, _COLOR_NEUTRAL: 3}
    if not items:
        return _COLOR_BRAND
    colors = [_tier_color(it) for it in items]
    return max(colors, key=lambda c: severity.get(c, 0))


def _status_color(request: dict, fallback: str) -> str:
    """Post-decision color (used by chat_update). When the status
    isn't a known terminal value we hand back `fallback` so colour
    survives intermediate states."""
    status = (request or {}).get("status")
    return {
        "completed":           _COLOR_BRAND,
        "approved":            _COLOR_BRAND,
        "scheduled":           _COLOR_BRAND,
        "executing":           _COLOR_BRAND,
        "rejected":            _COLOR_DANGER,
        "failed":              _COLOR_DANGER,
        "changes_requested":   _COLOR_AMBER,
        "awaiting_dba_manual": _COLOR_AMBER,
        "cancelled":           _COLOR_NEUTRAL,
    }.get(status, fallback)


def display_overrides() -> dict:
    """Return {'username': ..., 'icon_emoji': ...} for chat.postMessage so the
    bot shows up with a friendlier name and icon than its raw app name. Both
    are pulled from `bot_config` so they can be tuned via SQL UPDATE without a
    restart. Empty values are omitted."""
    out: dict = {}
    name = cfg.get_setting("bot_display_name", "").strip()
    icon = cfg.get_setting("bot_display_icon", "").strip()
    if name:
        out["username"] = name
    if icon:
        out["icon_emoji"] = icon
    return out


def _ensure_trailing_divider(blocks: list[dict] | None) -> list[dict] | None:
    """Append a full-width divider block to the end of a message so stacked
    DMs don't visually run into each other. No-op when there are no blocks
    or the message already ends with a divider."""
    if not blocks:
        return blocks
    if blocks[-1].get("type") == "divider":
        return blocks
    return [*blocks, {"type": "divider"}]


def _post(client, **kwargs):
    """chat.postMessage wrapper that adds a trailing divider to block
    messages. getattr() avoids the literal `_post(client,` so a
    blanket call-site rewrite can't recurse into this helper."""
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    if kwargs.get("blocks"):
        kwargs["blocks"] = _ensure_trailing_divider(kwargs["blocks"])
    return getattr(client, "chat_postMessage")(**kwargs)


def _update(client, **kwargs):
    """chat.update wrapper — same trailing-divider treatment as _post."""
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    if kwargs.get("blocks"):
        kwargs["blocks"] = _ensure_trailing_divider(kwargs["blocks"])
    return getattr(client, "chat_update")(**kwargs)


# Backward-compat alias (existing internal callers used the underscore form).
_display_overrides = display_overrides


# ---------- mrkdwn context helpers (used by both DMs and admin updates) ----------


def esc(text) -> str:
    """Escape requester-supplied text before it enters a mrkdwn block.

    Slack's own rule: & < > must be encoded, which also neutralises the
    `<@U123>` mention and `<url|label>` link syntaxes. The justification field
    was interpolated raw, so a requester could shape the DBA's approval card —
    a value like

        looks fine
        :white_check_mark: *Safety review: PASSED* - approved by <@U0ADMIN2>

    rendered as extra lines on the card, fake mention included, right beside
    the real Approve button. Approvals are decided by reading that card, so its
    text must not be attacker-controlled."""
    return (str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def esc_code(text) -> str:
    """Escape requester text destined for a ``` fenced block.

    Escaping alone is not enough here: a query containing its own triple
    backtick closes the fence, and everything after it renders as mrkdwn. A
    zero-width space inside the backtick run keeps the fence intact and looks
    identical to the reviewer."""
    return esc(text).replace("```", "`​``")


# ---------- presentation helpers (tier pill + Slack date) ----------------
#
# Two visual tweaks lifted from Slack's Block Kit template gallery.
# Both render inline in mrkdwn so we don't add new block types.


def _tier_emoji(tier: str | None) -> str:
    """Just the colon-emoji shortcode. Useful in plain_text blocks
    (Slack header blocks render emoji but not bold mrkdwn)."""
    t = (tier or "ro").lower()
    if t == "ddl":
        return ":red_circle:"
    if t == "rw":
        return ":large_yellow_circle:"
    return ":large_green_circle:"


def _tier_pill(tier: str | None) -> str:
    """🟢 *RO* / 🟡 *RW* / 🔴 *DDL* — emoji-prefixed tier badge for
    mrkdwn sections. More scannable than the old `[RO]` backticks;
    the colour cue echoes the attachment's left-edge bar."""
    return f"{_tier_emoji(tier)} *{(tier or 'ro').upper()}*"


def _slack_date(dt, fallback: str) -> str:
    """Render an aware datetime as Slack's `<!date^...>` placeholder.
    Slack rewrites this per viewer's profile timezone with
    Today / Tomorrow / Yesterday shortcuts via `{date_pretty}`.
    `fallback` is what shows if Slack can't substitute (rare —
    notifications, email digests). dt must be tz-aware."""
    if dt is None:
        return fallback
    try:
        epoch = int(dt.timestamp())
    except Exception:
        return fallback
    return f"<!date^{epoch}^{{date_pretty}} at {{time}}|{fallback}>"


def _fmt_result_format(request: dict) -> str:
    """Human label for the requester's pick of result file. Mirrors
    the modal's radio choices: CSV / Excel (.xlsx) / none."""
    if not request.get("wants_result"):
        return "_no file_"
    fmt = (request.get("result_format") or "csv").lower()
    return {"csv": "CSV", "xlsx": "Excel (.xlsx)"}.get(fmt, fmt.upper())


def hosting_md(target) -> str:
    """`AWS · RDS` — where the target actually runs, from its tag bag.

    Empty string when the connection carries no hosting tags, so an untagged
    fleet reads exactly as it did before. The account id is deliberately left
    out: this line exists to answer "which machine", and a cloud account
    number is registry detail an approver does not need in a DM.
    """
    tags = (getattr(target, "tags", None) or {}) if target else {}
    parts = [str(tags[k]) for k in ("provider", "service") if tags.get(k)]
    return "  •  ".join(f"`{p}`" for p in parts)


def request_context_md(request: dict) -> str:
    """One-line server+database summary for a /sql request DM."""
    target = targets.get(request["target_server_id"]) if request.get("target_server_id") else None
    target_alias = target.alias if target else "?"
    db = request.get("database_name") or "?"
    line = f"*Server:* `{target_alias}`  •  *Database:* `{db}`"
    # Where it runs, for the approver with the least context in the flow. An
    # approver saying yes to a DDL on the Huawei box is not saying yes to the
    # same thing as on AWS, and Slack is where that judgement is usually made.
    host = hosting_md(target)
    if host:
        line += f"  •  *Runs on:* {host}"
    return line


def request_context_with_query_md(request: dict, max_chars: int = 2500) -> str:
    """Server+database line + SQL code block. Used in DMs where the user
    needs to see what was submitted (rejected, failed, changes requested)."""
    base = request_context_md(request)
    q = (request.get("query") or "").strip()
    if not q:
        return base
    if len(q) > max_chars:
        q = q[:max_chars] + "\n... (truncated)"
    return f"{base}\n```\n{q}\n```"


def _request_blocks(
    request: dict, target: targets.TargetServer,
    *, with_actions: bool = True,
) -> list[dict]:
    """Block-kit body for the admin DM. The SQL itself is NOT inlined here —
    it goes in a thread reply as a file snippet (see `notify_admins`) so long
    queries render cleanly. The body contains: header, destructive warning
    (if any), metadata fields (with full target endpoint), and action buttons.
    """
    safety = query_safety.analyze(request["query"])

    # Compact card: one bold title line + a single-line meta context.
    # Endpoint / format / schedule / justification each get their own
    # tight row below — readable at a glance, no double-line "label
    # above value" pattern that ate vertical space before.
    title = f"*SQL request `#{request['id']}`*    {_tier_pill(safety.main_tier)}"
    if safety.is_destructive:
        title = f":rotating_light: {title} — DESTRUCTIVE"

    meta_bits = [
        f"<@{request['requester_slack_id']}>",
        "→",
        f"`{target.alias}` / `{request['database_name']}`",
    ]
    meta_line = " ".join(meta_bits)

    # Endpoint sits on its own context line — full RDS hostnames blow
    # past Slack's wrap width and push siblings (result / scheduled) to
    # ugly second-row alignment. Two short rows beats one long one.
    result_label = _fmt_result_format(request)
    # Which surface the request came from — so the admin sees at a glance
    # whether it was submitted from Slack or the web app.
    origin_label = origins.label(request.get("origin"))
    second_row_bits = [f"via {origin_label}", f"result: {result_label}"]
    sched = request.get("scheduled_for")
    if sched is not None:
        sched_fallback = f"{sched:%Y-%m-%d %H:%M UTC}"
        second_row_bits.append(f"scheduled: *{_slack_date(sched, sched_fallback)}*")

    blocks: list[dict] = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": title}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": meta_line}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": f"endpoint: `{target.host}:{target.port}`"}]},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": "  ·  ".join(second_row_bits)}]},
    ]
    # Static risk hint from the pre-flight EXPLAIN plan (seq scans / cost /
    # est rows). Informational only — the admin still decides. Omitted when
    # no plan was captured (DDL / transport fail-open / pre-flight off).
    risk = request.get("risk_summary")
    if risk:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": risk}],
        })
    if safety.is_destructive:
        kw_list = safety.keywords_found or ["WRITE"]
        kw = ", ".join(kw_list)
        # Tier-aware safety hint. UPDATE/DELETE → WHERE clause is the
        # critical lever; DML INSERT/MERGE → row content; DDL → schema
        # change is permanent. Generic "inspect intent" line fits all
        # the remainder.
        leading = kw_list[0].upper()
        if leading in ("UPDATE", "DELETE"):
            kind = "MODIFIES rows on the target"
            hint = ("Inspect the WHERE clause and confirm the intent "
                    "before clicking Approve.")
        elif leading in ("INSERT", "MERGE"):
            kind = "WRITES new rows on the target"
            hint = ("Inspect the row values being written and confirm "
                    "the intent before clicking Approve.")
        elif leading == "TRUNCATE":
            kind = "EMPTIES the target table"
            hint = ("This wipes ALL rows from the table; there is no "
                    "WHERE. Double-check the table name before clicking "
                    "Approve.")
        elif leading in ("DROP",):
            kind = "DROPS an object on the target"
            hint = ("This permanently removes the named object. "
                    "Double-check the object name before clicking Approve.")
        elif leading in ("ALTER", "CREATE", "GRANT", "REVOKE",
                         "COMMENT", "RENAME", "REFRESH", "REASSIGN",
                         "VACUUM", "ANALYZE", "REINDEX", "CLUSTER"):
            kind = "MODIFIES the schema / metadata on the target"
            hint = ("Inspect the object name and the change being made "
                    "before clicking Approve.")
        else:
            kind = "MODIFIES the target"
            hint = ("Inspect the statement and confirm the intent "
                    "before clicking Approve.")
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":warning: *This query {kind}.* "
                        f"Statement type: `{kw}`.\n"
                        f"Approving will execute it immediately and the "
                        f"bot cannot undo the change. {hint}"
                    ),
                },
            }
        )
    if request.get("justification"):
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn",
                              "text": f"_justification_: {esc(request['justification'])}"}],
            }
        )
    query_text = request.get("query") or ""
    if 0 < len(query_text) <= INLINE_QUERY_MAX_CHARS:
        # Short enough — show inline as a fenced code block.
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```\n{esc_code(query_text)}\n```"},
        })
    else:
        # Long: pointer to the thread-attached file snippet (uploaded by
        # notify_admins after the postMessage). Code path also covers the
        # empty-query case, which shouldn't happen in practice.
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": ":page_facing_up: _Full SQL is attached as a snippet in the thread below._",
            }],
        })
    request_id = str(request["id"])
    if safety.is_destructive:
        approve_confirm = {
            "title": {"type": "plain_text", "text": "Approve destructive query?"},
            "text": {
                "type": "mrkdwn",
                "text": f":warning: This is a *{', '.join(safety.keywords_found)}* "
                        f"statement. It will modify data immediately and cannot be "
                        f"undone by the bot. Confirm only if you have read the WHERE "
                        f"clause and accept the impact.",
            },
            "confirm": {"type": "plain_text", "text": "Yes, run it"},
            "deny": {"type": "plain_text", "text": "Wait"},
        }
    else:
        approve_confirm = {
            "title": {"type": "plain_text", "text": "Approve and run?"},
            "text": {"type": "mrkdwn", "text": "This will execute the query immediately."},
            "confirm": {"type": "plain_text", "text": "Run it"},
            "deny": {"type": "plain_text", "text": "Wait"},
        }
    if with_actions:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"req_{request_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": ACTION_APPROVE,
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "value": request_id,
                        "confirm": approve_confirm,
                    },
                    {
                        "type": "button",
                        "action_id": ACTION_REJECT,
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "value": request_id,
                    },
                    {
                        "type": "button",
                        "action_id": ACTION_REQUEST_CHANGES,
                        "text": {"type": "plain_text", "text": "Request changes"},
                        "value": request_id,
                    },
                ],
            }
        )
    else:
        # View-only mode: admin gets the DM (audit / transparency) but
        # cannot act on this request because their scope (tier / target
        # / team) doesn't admit it. Another in-scope admin will handle.
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (":eyes: _View only — this request is outside your "
                         "admin scope (tier / target / team). Another admin "
                         "with broader scope will handle it._"),
            }],
        })
    return blocks


def _resolved_blocks(request: dict, target: targets.TargetServer, status_line: str) -> list[dict]:
    # Section (not context) — without the colored left bar to convey
    # decision, the status line needs full-size prominence.
    blocks = _request_blocks(request, target)[:-1]  # drop the action block
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": status_line},
        }
    )
    return blocks


def _scheduled_blocks(
    request: dict, target: targets.TargetServer, status_line: str,
) -> list[dict]:
    """Variant of _resolved_blocks for scheduled requests: keeps an action
    block with a Cancel button instead of just a status context."""
    blocks = _request_blocks(request, target)[:-1]  # drop original action block
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": status_line},
    })
    blocks.append(_cancel_action_block(request))
    return blocks


def _dba_manual_blocks(
    request: dict, target: targets.TargetServer, status_line: str,
) -> list[dict]:
    """Admin-DM blocks while a DDL request is awaiting DBA manual
    execution. Keeps the original request context and adds two action
    buttons (Mark completed / Mark failed) so the DBA can close out
    the request once they've run the query out-of-band."""
    blocks = _request_blocks(request, target)[:-1]
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": status_line},
    })
    blocks.append({
        "type": "actions",
        "block_id": f"req_dba_{request['id']}",
        "elements": [
            {
                "type": "button",
                "action_id": ACTION_DBA_MARK_COMPLETED,
                "style": "primary",
                "text": {"type": "plain_text", "text": "Mark completed"},
                "value": str(request["id"]),
                "confirm": {
                    "title": {"type": "plain_text", "text": "Mark completed?"},
                    "text": {"type": "mrkdwn", "text":
                             "Confirm you've run this DDL out-of-band "
                             "and it succeeded."},
                    "confirm": {"type": "plain_text", "text": "Yes, completed"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            },
            {
                "type": "button",
                "action_id": ACTION_DBA_MARK_FAILED,
                "style": "danger",
                "text": {"type": "plain_text", "text": "Mark failed"},
                "value": str(request["id"]),
            },
        ],
    })
    return blocks


def _cancel_action_block(request: dict) -> dict:
    return {
        "type": "actions",
        "block_id": f"req_cancel_{request['id']}",
        "elements": [{
            "type": "button",
            "action_id": ACTION_CANCEL_SCHEDULED,
            "style": "danger",
            "text": {"type": "plain_text", "text": "Cancel scheduled run"},
            "value": str(request["id"]),
            "confirm": {
                "title": {"type": "plain_text", "text": "Cancel scheduled run?"},
                "text": {"type": "mrkdwn",
                         "text": "The scheduled execution will be skipped. This is final."},
                "confirm": {"type": "plain_text", "text": "Yes, cancel"},
                "deny": {"type": "plain_text", "text": "Keep"},
            },
        }],
    }


def _upload_query_snippet(
    client: WebClient,
    channel_id: str,
    request_id: int,
    query: str,
    thread_ts: str,
) -> None:
    """Reply in thread with the full SQL as a `.sql` file snippet. Slack
    renders these inline with syntax highlighting and is the only graceful
    way to show queries longer than ~3KB. Runs after the main DM is posted;
    failure is logged but does not abort the request flow."""
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    try:
        client.files_upload_v2(
            channel=channel_id,
            content=query,
            filename=f"request_{request_id}.sql",
            title=f"SQL for request #{request_id}",
            snippet_type="sql",
            thread_ts=thread_ts,
        )
    except SlackApiError as e:
        log.warning(
            "files_upload_v2 (snippet) failed for request %s: %s",
            request_id, e.response.get("error") if e.response else e,
        )
        # Fallback so the admin still has the SQL to read.
        try:
            _post(client,
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"```\n{query[:3000]}\n```"
                + (" _(truncated)_" if len(query) > 3000 else ""),
                **_display_overrides(),
            )
        except Exception:
            log.exception("Snippet fallback also failed for request %s", request_id)


# ===========================================================================
# Bundle (multi-query batch) admin notifications
# ===========================================================================
#
# A bundle is a group of N `requests` rows submitted together via `/sql batch`.
# Each admin gets ONE DM that covers every item: per-item card with its own
# Approve / Reject / Changes buttons (re-using the existing single-item action
# handlers — the buttons carry the per-item request_id as `value`, so no
# handler-level changes are required for per-item decisions).


_BUNDLE_INLINE_QUERY_MAX = 500  # shorter than single-shot — keep the DM compact


def _bundle_status_label(status: str) -> str:
    """Human-facing label for a bundle status. The DB keeps 'decided'
    (trigger + recompute logic depend on it); users read 'finished' —
    neutral wording that fits both all-completed and all-rejected
    outcomes."""
    return {"decided": "finished"}.get(status, status)


def _bundle_item_status_line(item: dict) -> str | None:
    """One-line status summary for a terminal/in-flight item inside a
    bundle DM. Returns None when the item is still pending (caller will
    render the action buttons instead)."""
    status = item.get("status")
    if status == "pending":
        return None
    decider = item.get("decided_by_slack_id")
    reason = item.get("decision_reason")
    row_count = item.get("row_count")
    err = (item.get("error_message") or "").strip()
    if err:
        err = err.replace("\n", " ")
        if len(err) > 200:
            err = err[:199] + "…"
    # Who approved it — manual admin (<@id>) or the auto-approver. Shown on
    # every post-approval state so the bundle summary carries the same
    # accountability the single-request DMs do, not just on rejection.
    if decider == "AUTO":
        appr = " · _auto-approved_"
    elif decider:
        appr = f" · approved by <@{decider}>"
    else:
        appr = ""
    if status == "approved":
        return f":hourglass_flowing_sand: _Approved, waiting to execute._{appr}"
    if status == "scheduled":
        return f":alarm_clock: _Approved and scheduled._{appr}"
    if status == "executing":
        return f":runner: _Executing now…_{appr}"
    if status == "completed":
        rc = f"{row_count:,} row(s)" if row_count is not None else "no result set"
        if item.get("truncated"):
            rc += " (showing first rows — result was larger)"
        return f":white_check_mark: _Completed — {rc}._{appr}"
    if status == "failed":
        return f":x: _Failed: {err or '(no detail)'}_"
    if status == "rejected":
        decider_md = f"<@{decider}>" if decider else "an admin"
        bit = f" — {reason}" if reason else ""
        return f":no_entry: _Rejected by {decider_md}{bit}._"
    if status == "cancelled":
        return ":no_entry_sign: _Cancelled._"
    if status == "changes_requested":
        bit = f" — {reason}" if reason else ""
        return f":pencil2: _Changes requested{bit}._"
    if status == "awaiting_dba_manual":
        return ":construction: _Awaiting manual DBA execution._"
    return f"_Status: {status}._"


def _bundle_item_blocks(
    item: dict, *, in_scope: bool, item_index: int, total: int,
) -> list[dict]:
    """Block-kit for one item inside a bundle DM. Renders header,
    target/db summary, query (truncated for long), and either the
    per-item action buttons (in_scope) or a view-only context line.

    Item shape: rows from bundles.list_items() — has `target_alias`,
    `target_host`, `database_name`, `query`, `position`, `id`, `status`,
    `wants_result`. The action buttons fire the existing single-request
    handlers (ACTION_APPROVE / REJECT / REQUEST_CHANGES), so the value
    is the item's request_id."""
    safety = query_safety.analyze(item.get("query") or "")
    tier_label = query_safety.required_mode(item.get("query") or "").upper()
    header_text = (
        f"Item #{item_index} of {total}  ·  {item['target_alias']}  ·  "
        f"{_tier_emoji(tier_label)} {tier_label}"
    )
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": header_text[:150]}},
        {"type": "context",
         "elements": [{"type": "plain_text",
                       "text": f"db: {item['database_name']}  ·  request #{item['id']}",
                       "emoji": False}]},
    ]
    if safety.is_destructive:
        kw = ", ".join(safety.keywords_found or ["WRITE"])
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f":warning: *Destructive ({kw}).* Inspect carefully before approving."},
        })

    # Static risk hint from the pre-flight EXPLAIN plan (size + cost band).
    risk = item.get("risk_summary")
    if risk:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": risk}],
        })

    query_text = (item.get("query") or "").strip()
    if 0 < len(query_text) <= _BUNDLE_INLINE_QUERY_MAX:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```\n{esc_code(query_text)}\n```"},
        })
    elif len(query_text) > _BUNDLE_INLINE_QUERY_MAX:
        preview = query_text[:_BUNDLE_INLINE_QUERY_MAX] + "\n... (truncated; full query stored on request #" + str(item['id']) + ")"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```\n{esc_code(preview)}\n```"},
        })

    request_id = str(item["id"])

    # awaiting_dba_manual: the bot escalated this DDL for manual execution
    # (e.g. the role doesn't own the table). Show the status line PLUS
    # [Mark completed]/[Mark failed] so an admin can close it straight from the
    # bundle DM after running it out-of-band. Single-request escalations always
    # had these buttons; bundle items didn't — this closes that gap. The
    # handlers (handle_dba_mark_*) already accept bundle items by request_id.
    if item.get("status") == "awaiting_dba_manual":
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _bundle_item_status_line(item)}],
        })
        if in_scope:
            blocks.append({
                "type": "actions",
                "block_id": f"req_dba_{request_id}",
                "elements": [
                    {"type": "button", "style": "primary",
                     "action_id": ACTION_DBA_MARK_COMPLETED,
                     "text": {"type": "plain_text", "text": "Mark completed"},
                     "value": request_id},
                    {"type": "button", "style": "danger",
                     "action_id": ACTION_DBA_MARK_FAILED,
                     "text": {"type": "plain_text", "text": "Mark failed"},
                     "value": request_id},
                ],
            })
        else:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn",
                              "text": ":eyes: _Outside your admin scope — another admin will close this._"}],
            })
        blocks.append({"type": "divider"})
        return blocks

    # Terminal / in-flight items: show a status line instead of buttons,
    # so admins glancing at the DM later see what happened without
    # scrolling through stale buttons. Only `pending` items keep buttons.
    status_line = _bundle_item_status_line(item)
    if status_line is not None:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": status_line}],
        })
        blocks.append({"type": "divider"})
        return blocks

    if in_scope:
        approve_confirm = {
            "title": {"type": "plain_text", "text": "Approve and run?"},
            "text": {"type": "mrkdwn",
                     "text": (
                         f":warning: Destructive ({', '.join(safety.keywords_found)}). "
                         "This runs immediately and the bot cannot undo it."
                         if safety.is_destructive
                         else "This will execute immediately."
                     )},
            "confirm": {"type": "plain_text",
                        "text": "Yes, run it" if safety.is_destructive else "Run it"},
            "deny": {"type": "plain_text", "text": "Wait"},
        }
        blocks.append({
            "type": "actions",
            "block_id": f"req_{request_id}",
            "elements": [
                {"type": "button",
                 "action_id": ACTION_APPROVE,
                 "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve"},
                 "value": request_id,
                 "confirm": approve_confirm},
                {"type": "button",
                 "action_id": ACTION_REJECT,
                 "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject"},
                 "value": request_id},
                {"type": "button",
                 "action_id": ACTION_REQUEST_CHANGES,
                 "text": {"type": "plain_text", "text": "Request changes"},
                 "value": request_id},
            ],
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (":eyes: _Outside your admin scope — another admin "
                         "will handle this item._"),
            }],
        })
    blocks.append({"type": "divider"})
    return blocks


def notify_admins_bundle(client: WebClient, bundle_id: int) -> None:
    """Fan out a single DM per admin covering every item in the bundle.
    Per-item Approve / Reject / Changes buttons re-use the existing
    single-request action handlers (the button value carries the item's
    request_id). One row written to request_notifications per admin
    with `bundle_id` set + `request_id` NULL, so chat.update on the
    bundle DM is addressable later (PR2)."""
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    from .. import bundles as bundles_mod

    with db.connection() as conn, conn.cursor() as cur:
        bundle = bundles_mod.get_bundle(cur, bundle_id)
        if bundle is None:
            log.warning("notify_admins_bundle: bundle %s not found", bundle_id)
            return
        items = bundles_mod.list_items(cur, bundle_id)
    if not items:
        log.warning("notify_admins_bundle: bundle %s has 0 items", bundle_id)
        return

    overrides = display_overrides()
    for admin in admins.list_active():
        admin_id = admin["slack_user_id"]
        blocks = _build_bundle_dm_blocks(bundle, items, admin_id)
        try:
            opened = client.conversations_open(users=admin_id)
            channel_id = opened["channel"]["id"]
            posted = _post(client,
                channel=channel_id,
                blocks=blocks,
                text=f"SQL batch B#{bundle_id}",
                **overrides,
            )
            db.execute(
                "INSERT INTO request_notifications "
                "(request_id, bundle_id, admin_slack_id, channel_id, message_ts) "
                "VALUES (NULL, %s, %s, %s, %s)",
                (bundle_id, admin_id, channel_id, posted["ts"]),
            )
        except Exception:
            log.exception(
                "Failed to DM admin %s about bundle %s", admin_id, bundle_id,
            )


ACTION_BUNDLE_APPROVE_ALL = "act_bundle_approve_all"
ACTION_BUNDLE_REJECT_ALL  = "act_bundle_reject_all"

ACTION_IMPORT_APPROVE = "act_import_approve"
ACTION_IMPORT_REJECT  = "act_import_reject"

ACTION_RESUBMIT = "act_resubmit"
ACTION_FAVORITE = "act_favorite"


def favorite_action_block(request_id: int) -> dict:
    """A '⭐ Favorite this query' button block. Appended to a completed
    request's result DM so the requester can one-click star the query for
    later reload from the /sql modal's favorites picker."""
    return {
        "type": "actions", "block_id": f"favorite_{request_id}",
        "elements": [{
            "type": "button", "action_id": ACTION_FAVORITE,
            "text": {"type": "plain_text", "text": "⭐ Favorite this query"},
            "value": str(request_id),
        }],
    }


def favorite_followup(client: WebClient, principal_id: str,
                      request_id: int) -> None:
    """Post a compact '⭐ Favorite this query' follow-up to the requester
    after a completed request, so they can one-click star it. Best-effort."""
    try:
        dm_requester(
            client, principal_id,
            "Reuse this query later?",
            blocks=[
                {"type": "section",
                 "text": {"type": "mrkdwn", "text": ":star: *Reuse this query later?*"}},
                favorite_action_block(request_id),
            ],
        )
    except Exception:
        log.exception("favorite_followup failed (req=%s)", request_id)


def resubmit_action_block(request_id: int) -> dict:
    """An 'Edit & resubmit' button block. Appended to the failed /
    rejected / changes-requested requester DMs so the user can reopen a
    pre-filled modal instead of retyping everything."""
    return {
        "type": "actions", "block_id": f"resubmit_{request_id}",
        "elements": [{
            "type": "button", "action_id": ACTION_RESUBMIT,
            "text": {"type": "plain_text", "text": "✏️ Edit & resubmit"},
            "value": str(request_id),
        }],
    }


def resubmit_blocks(text: str, request_id: int) -> list[dict]:
    """A requester DM body (mrkdwn) + the resubmit button. For DMs that
    pass plain text (e.g. the failed DM)."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        resubmit_action_block(request_id),
    ]


# ===========================================================================
# CSV import admin notifications
# ===========================================================================


def _import_dm_blocks(imp: dict, parsed, *, with_actions: bool) -> list[dict]:
    """Admin DM for a pending CSV import: target/table, stats, columns,
    and a PII-masked sample preview, plus Approve/Reject buttons.
    `parsed` is a csv_import.ParsedCsv (carries the sample rows)."""
    from .. import csv_import, pii
    target = targets.get(imp["target_server_id"])
    alias = target.alias if target else f"target#{imp['target_server_id']}"
    table = imp["table_name"]
    kind = "NEW table" if imp["is_new_table"] else "existing table"
    persistence = ""
    if imp["is_new_table"]:
        persistence = " · UNLOGGED (temp)" if imp["unlogged"] else " · LOGGED"
    delim_name = {",": "comma", ";": "semicolon", "\t": "tab"}.get(imp["delimiter"], imp["delimiter"])

    cols = imp["columns"]
    title = f"*CSV import `#{imp['id']}`*  :inbox_tray:"
    meta = f"<@{imp['requester_slack_id']}> → `{alias}` / `{imp['database_name']}`"
    table_line = (f"target: `dba.{table}` ({kind}{persistence})")
    stats = (f"{imp.get('row_count', 0):,} rows · {len(cols)} cols · "
             f"{(imp.get('byte_size') or 0) // 1024} KB · delimiter: {delim_name}")

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": meta}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": table_line}]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": stats}]},
    ]

    cols_str = ", ".join(f"`{c}`" for c in cols[:30])
    if len(cols) > 30:
        cols_str += f" … (+{len(cols) - 30})"
    blocks.append({"type": "section",
                   "text": {"type": "mrkdwn", "text": f"*Columns:* {cols_str}"}})

    # For a new table, show the exact CREATE TABLE the import will run so
    # the admin approves the real DDL, not just a column summary.
    if imp["is_new_table"]:
        ddl = csv_import.create_table_preview(
            table, imp["unlogged"], cols, imp.get("column_defs"))
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": f"*Will run:*\n```\n{ddl}\n```"}})
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"_Appends into existing `dba.{table}` (COPY, no DDL)._"}]})

    # PII-masked sample preview.
    if parsed is not None and getattr(parsed, "sample_rows", None):
        found: set[str] = set()
        cmap = pii.column_pii_map(cols)
        lines = [" | ".join(cols)]
        for r in parsed.sample_rows:
            masked = pii.mask_row(r, found, cmap)
            lines.append(" | ".join(str(v) for v in masked))
        preview = "\n".join(lines)[:1500]
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": f"*Sample (PII-masked):*\n```\n{preview}\n```"}})

    if with_actions:
        confirm = {
            "title": {"type": "plain_text", "text": "Approve CSV import?"},
            "text": {"type": "mrkdwn",
                     "text": f":warning: This bulk-loads {imp.get('row_count',0):,} "
                             f"rows into `dba.{table}`. This writes data and "
                             f"cannot be undone by the bot."},
            "confirm": {"type": "plain_text", "text": "Yes, import"},
            "deny": {"type": "plain_text", "text": "Wait"},
        }
        blocks.append({
            "type": "actions",
            "block_id": f"import_{imp['id']}",
            "elements": [
                {"type": "button", "action_id": ACTION_IMPORT_APPROVE,
                 "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve import"},
                 "value": str(imp["id"]), "confirm": confirm},
                {"type": "button", "action_id": ACTION_IMPORT_REJECT,
                 "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject"},
                 "value": str(imp["id"])},
            ],
        })
    return blocks


def _import_resolved_blocks(imp: dict, status_line: str) -> list[dict]:
    """Post-decision import DM: header + target + a status line (no
    buttons, no sample — rendered from the imp row alone, so the executor
    can call it without the parsed CSV)."""
    target = targets.get(imp["target_server_id"])
    alias = target.alias if target else f"target#{imp['target_server_id']}"
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*CSV import `#{imp['id']}`*  :inbox_tray:"}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"<@{imp['requester_slack_id']}> → `{alias}` / "
                          f"`{imp['database_name']}` → `dba.{imp['table_name']}`"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": status_line}},
    ]


def notify_admins_import(client: WebClient, imp: dict, parsed) -> None:
    """DM every active admin a pending CSV import with Approve/Reject.
    Records (channel, ts) per admin in import_notifications for lockstep
    chat.update on decision."""
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    blocks = _import_dm_blocks(imp, parsed, with_actions=True)
    overrides = display_overrides()
    for admin in admins.list_active():
        aid = admin["slack_user_id"]
        try:
            opened = client.conversations_open(users=aid)
            ch = opened["channel"]["id"]
            posted = _post(client,
                channel=ch, blocks=blocks, text=f"CSV import #{imp['id']}",
                **overrides,
            )
            db.execute(
                "INSERT INTO import_notifications "
                "(import_id, admin_slack_id, channel_id, message_ts) "
                "VALUES (%s, %s, %s, %s)",
                (imp["id"], aid, ch, posted["ts"]),
            )
        except Exception:
            log.exception("Failed to DM admin %s about import %s", aid, imp["id"])


def update_import_admin_messages(client: WebClient, imp: dict, status_line: str) -> None:
    """Replace every admin's import DM with a resolved status line."""
    blocks = _import_resolved_blocks(imp, status_line)
    rows = db.fetch_all(
        "SELECT channel_id, message_ts FROM import_notifications WHERE import_id=%s",
        (imp["id"],),
    )
    for r in rows:
        try:
            _update(client,channel=r["channel_id"], ts=r["message_ts"],
                               blocks=blocks, text=status_line)
        except Exception:
            log.exception("Failed to update import DM for %s", imp["id"])


def _bundle_bulk_action_blocks(bundle_id: int,
                               any_pending: bool,
                               any_pending_in_scope: bool) -> list[dict]:
    """Two bulk buttons under the per-item cards.

    - Hidden when no item is pending anywhere (bundle is fully decided).
    - When pending items exist but none are in this admin's scope, we
      render a context line instead so the admin understands why no
      bulk button shows up for them.
    """
    if not any_pending:
        return []
    if not any_pending_in_scope:
        return [{
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (":eyes: _All remaining items are outside your scope; "
                         "another admin will close this bundle._"),
            }],
        }]
    confirm = {
        "title": {"type": "plain_text", "text": "Decide remaining items?"},
        "text": {"type": "mrkdwn",
                 "text": "This applies to every item still pending in your scope."},
        "confirm": {"type": "plain_text", "text": "Do it"},
        "deny": {"type": "plain_text", "text": "Wait"},
    }
    return [{
        "type": "actions",
        "block_id": f"bundle_bulk_{bundle_id}",
        "elements": [
            {"type": "button",
             "action_id": ACTION_BUNDLE_APPROVE_ALL,
             "style": "primary",
             "text": {"type": "plain_text", "text": "Approve all remaining (in scope)"},
             "value": str(bundle_id),
             "confirm": confirm},
            {"type": "button",
             "action_id": ACTION_BUNDLE_REJECT_ALL,
             "style": "danger",
             "text": {"type": "plain_text", "text": "Reject all remaining (in scope)"},
             "value": str(bundle_id),
             "confirm": confirm},
        ],
    }]


def _build_bundle_dm_blocks(bundle: dict, items: list[dict],
                            admin_id: str) -> list[dict]:
    """Render the bundle DM as seen by one admin. Per-item buttons are
    gated by admins.can_approve(); the bulk-button block at the bottom
    is also scope-aware."""
    requester_md = f"<@{bundle['requester_slack_id']}>"
    sched_md = ""
    if bundle.get("scheduled_for"):
        sched_md = f"  ·  *scheduled* `{bundle['scheduled_for']:%Y-%m-%d %H:%M UTC}`"
    status = bundle.get("status", "pending")
    header_status_emoji = {
        "pending":   ":hourglass:",
        "partial":   ":large_yellow_circle:",
        "decided":   ":white_check_mark:",
        "cancelled": ":no_entry_sign:",
    }.get(status, "")
    header_blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text",
                  "text": f"SQL batch B#{bundle['id']} ({len(items)} item(s))"[:150]}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"From {requester_md}{sched_md}  ·  bundle status: "
                          f"{header_status_emoji} `{_bundle_status_label(status)}`"}},
    ]
    if bundle.get("justification"):
        header_blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*Justification*\n{esc(bundle['justification'])}"},
        })
    header_blocks.append({"type": "divider"})

    any_pending = False
    any_pending_in_scope = False
    item_blocks: list[dict] = []
    for it in items:
        item_for_scope = {
            "id": it["id"],
            "query": it["query"],
            "target_server_id": it["target_server_id"],
            "requester_slack_id": bundle["requester_slack_id"],
        }
        in_scope = admins.can_approve(admin_id, item_for_scope)
        if it["status"] == "pending":
            any_pending = True
            if in_scope:
                any_pending_in_scope = True
        item_blocks.extend(_bundle_item_blocks(
            it, in_scope=in_scope, item_index=it["position"], total=len(items),
        ))

    bulk = _bundle_bulk_action_blocks(bundle["id"], any_pending, any_pending_in_scope)
    blocks = header_blocks + item_blocks + bulk

    if len(blocks) > 50:
        blocks = blocks[:49] + [{
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": ":warning: _Some items truncated — see the bot DB._"}],
        }]
    return blocks


def update_bundle_admin_dms(client: WebClient, bundle_id: int) -> None:
    """Re-render and chat.update every admin DM for this bundle.
    Called after any per-item state transition so item buttons collapse
    to a status line and out-of-scope items stay consistent across
    every admin's copy."""
    from .. import bundles as bundles_mod
    with db.connection() as conn, conn.cursor() as cur:
        bundle = bundles_mod.get_bundle(cur, bundle_id)
        if bundle is None:
            return
        items = bundles_mod.list_items(cur, bundle_id)
    rows = db.fetch_all(
        "SELECT admin_slack_id, channel_id, message_ts "
        "FROM request_notifications WHERE bundle_id = %s",
        (bundle_id,),
    )
    for r in rows:
        blocks = _build_bundle_dm_blocks(bundle, items, r["admin_slack_id"])
        try:
            _update(client,
                channel=r["channel_id"],
                ts=r["message_ts"],
                blocks=blocks,
                text=f"SQL batch B#{bundle_id}",
            )
        except Exception:
            log.exception(
                "Failed to update bundle DM admin=%s bundle=%s",
                r["admin_slack_id"], bundle_id,
            )

    # If the bundle just reached a terminal state, fire the requester
    # summary DM. Idempotent — re-uses the existing message_ts on
    # subsequent calls.
    if _is_bundle_terminal(bundle["status"]):
        maybe_send_bundle_summary(client, bundle_id)


def _fmt_bundle_item_summary_line(item: dict) -> str:
    """Single line in the requester's bundle summary DM. Compact:
    status emoji + alias / db + row count + duration / error preview."""
    from ..executor import _fmt_count, _fmt_duration
    status = item.get("status")
    glyph = {
        "completed":           ":white_check_mark:",
        "failed":              ":x:",
        "rejected":            ":no_entry:",
        "cancelled":           ":no_entry_sign:",
        "changes_requested":   ":pencil2:",
        "awaiting_dba_manual": ":construction:",
    }.get(status, ":grey_question:")
    parts: list[str] = [
        glyph,
        f"*#{item['position']}*",
        f"`{item['target_alias']}`/`{item['database_name']}`",
        f"_{status}_",
    ]
    rc = item.get("row_count")
    if rc is not None:
        cap_note = "+ (capped at max_rows)" if item.get("truncated") else ""
        parts.append(f"— {_fmt_count(rc)}{cap_note} row(s)")
    if item.get("executed_at") and item.get("completed_at"):
        dur = (item["completed_at"] - item["executed_at"]).total_seconds()
        parts.append(f"({_fmt_duration(dur)})")
    err = (item.get("error_message") or "").strip()
    if err:
        err = err.replace("\n", " ")
        if len(err) > 200:
            err = err[:199] + "…"
        parts.append(f"\n     └ _{err}_")
    return " ".join(parts)


def _is_bundle_terminal(status: str) -> bool:
    return status in {"decided", "partial", "cancelled"}


def maybe_send_bundle_summary(client: WebClient, bundle_id: int) -> None:
    """If the bundle has reached a terminal state and we haven't sent
    the summary DM yet, post it now (with any completed-item CSVs
    attached). Idempotent — once the summary message_ts is recorded on
    the bundle row, subsequent calls update that same message instead
    of spamming."""
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    from .. import bundles as bundles_mod
    bundle = db.fetch_one(
        "SELECT id, requester_slack_id, status, justification, "
        "       scheduled_for, "
        "       requester_summary_channel_id, requester_summary_message_ts "
        "  FROM request_bundles WHERE id = %s",
        (bundle_id,),
    )
    if bundle is None or not _is_bundle_terminal(bundle["status"]):
        return

    with db.connection() as conn, conn.cursor() as cur:
        items = bundles_mod.list_items(cur, bundle_id)
    if not items:
        return

    requester = bundle["requester_slack_id"]
    completed = sum(1 for it in items if it["status"] == "completed")
    failed = sum(1 for it in items if it["status"] == "failed")
    rejected = sum(1 for it in items if it["status"] == "rejected")
    cancelled = sum(1 for it in items if it["status"] == "cancelled")
    awaiting = sum(1 for it in items if it["status"] == "awaiting_dba_manual")

    overall = bundle["status"]
    head_emoji = {"decided": ":white_check_mark:",
                  "partial": ":large_yellow_circle:",
                  "cancelled": ":no_entry_sign:"}.get(overall, ":grey_question:")
    head_line = (
        f"{head_emoji} *SQL batch `B#{bundle_id}` {_bundle_status_label(overall)}* — "
        f"{len(items)} item(s): "
        f"{completed} completed, {failed} failed, {rejected} rejected, "
        f"{cancelled} cancelled"
        + (f", {awaiting} awaiting manual DBA" if awaiting else "")
        + "."
    )
    body_lines = [_fmt_bundle_item_summary_line(it) for it in items]
    text = head_line + "\n" + "\n".join(body_lines)
    blocks = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": head_line}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "\n".join(body_lines)[:2800]}},
    ]

    overrides = display_overrides()
    channel_id = bundle.get("requester_summary_channel_id")
    message_ts = bundle.get("requester_summary_message_ts")
    try:
        if message_ts and channel_id:
            # Already posted — just update in place (rare second wave).
            _update(client,
                channel=channel_id, ts=message_ts, text=text, blocks=blocks,
            )
        else:
            opened = client.conversations_open(users=requester)
            channel_id = opened["channel"]["id"]
            posted = _post(client,
                channel=channel_id, text=text, blocks=blocks, **overrides,
            )
            message_ts = posted["ts"]
            db.execute(
                "UPDATE request_bundles SET "
                " requester_summary_channel_id = %s, "
                " requester_summary_message_ts = %s "
                "WHERE id = %s",
                (channel_id, message_ts, bundle_id),
            )

            # Upload any CSVs that completed items left behind. Each one
            # goes as a follow-up message on the same DM channel — Slack
            # threads aren't ideal for DMs (clients hide them), so we
            # just post sequentially.
            for it in items:
                if it["status"] != "completed":
                    continue
                # csv_file_path is filled by the executor's
                # _complete_with_csv / _complete_multi paths.
                csv_path = db.fetch_one(
                    "SELECT csv_file_path FROM requests WHERE id = %s",
                    (it["id"],),
                ) or {}
                path = csv_path.get("csv_file_path")
                if not path:
                    continue
                try:
                    upload = client.files_upload_v2(
                        channel=channel_id,
                        file=path,
                        filename=f"B{bundle_id}_item{it['position']}_{it['target_alias']}.csv",
                        title=f"B#{bundle_id} item #{it['position']} result",
                        initial_comment=f":page_facing_up: Result for *item #{it['position']}* — `{it['target_alias']}/{it['database_name']}`",
                    )
                    file_id = None
                    if upload.get("files"):
                        file_id = upload["files"][0].get("id")
                    elif upload.get("file"):
                        file_id = upload["file"].get("id")
                    if file_id:
                        db.execute(
                            "UPDATE requests SET slack_file_id = %s WHERE id = %s",
                            (file_id, it["id"]),
                        )
                except Exception:
                    log.exception(
                        "bundle summary: failed to upload CSV for item %s",
                        it["id"],
                    )
    except Exception:
        log.exception(
            "Failed to send bundle summary DM for bundle %s", bundle_id,
        )
        return

    # Bundle-level rating: prompt once per bundle, anchored to the
    # first item's request_id. ratings.maybe_prompt's has_rating check
    # + per-user cooldown make this idempotent and survey-fatigue safe.
    # We only fire it on the FIRST summary send (message_ts was just
    # written); subsequent updates skip.
    if bundle.get("requester_summary_message_ts") is None and items:
        from .. import ratings as ratings_mod
        first = dict(items[0])
        first["requester_slack_id"] = requester
        try:
            ratings_mod.maybe_prompt(client, first)
        except Exception:
            log.exception(
                "rating prompt failed for bundle %s", bundle_id,
            )


def maybe_update_bundle_for_request(client: WebClient, request_id: int) -> None:
    """Lookup helper: if `request_id` belongs to a bundle, refresh that
    bundle's admin DMs. Safe to call from any single-request handler —
    it's a no-op for non-bundle requests."""
    row = db.fetch_one(
        "SELECT bundle_id FROM requests WHERE id = %s", (request_id,),
    )
    if not row or not row.get("bundle_id"):
        return
    update_bundle_admin_dms(client, row["bundle_id"])


def notify_admins(client: WebClient, request: dict) -> None:
    """DM every active admin with action buttons + a thread reply containing
    the full SQL as a `.sql` snippet. Records each (channel, ts) for later
    chat.update lockstep on decision."""
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    target = targets.get(request["target_server_id"])
    if target is None:
        raise LookupError("target server vanished between submit and notify")
    overrides = _display_overrides()

    for admin in admins.list_active():
        admin_id = admin["slack_user_id"]
        # Scope check: in-scope admins see action buttons; out-of-scope
        # admins still receive the DM (audit / transparency) but in
        # view-only form.
        in_scope = admins.can_approve(admin_id, request)
        blocks = _request_blocks(request, target, with_actions=in_scope)
        try:
            opened = client.conversations_open(users=admin_id)
            channel_id = opened["channel"]["id"]
            # Top-level blocks (not wrapped in an attachment). Slack
            # collapses long attachments behind a "Show more" link;
            # top-level blocks render in full. The `text` field is a
            # short notification fallback; Slack does NOT print it
            # above top-level blocks.
            posted = _post(client,
                channel=channel_id,
                blocks=blocks,
                text=f"SQL request #{request['id']}",
                **overrides,
            )
            db.execute(
                "INSERT INTO request_notifications "
                "(request_id, admin_slack_id, channel_id, message_ts) "
                "VALUES (%s, %s, %s, %s)",
                (request["id"], admin_id, channel_id, posted["ts"]),
            )
            # Only upload the thread snippet for long queries — short ones
            # are rendered inline above.
            if len(request.get("query") or "") > INLINE_QUERY_MAX_CHARS:
                _upload_query_snippet(
                    client, channel_id, request["id"], request["query"], posted["ts"],
                )
        except Exception:
            log.exception("Failed to DM admin %s about request %s", admin_id, request["id"])


def update_all_admin_messages(
    client: WebClient,
    request: dict,
    status_line: str,
    *,
    keep_cancel_button: bool = False,
    dba_manual: bool = False,
) -> None:
    """Replace the action-button block on every admin's DM with a status
    line. Variants:
      - keep_cancel_button: attaches [Cancel scheduled run] action block
        (transition to status='scheduled').
      - dba_manual: attaches [Mark completed] [Mark failed] action block
        (transition to status='awaiting_dba_manual')."""
    target = targets.get(request["target_server_id"])
    if target is None:
        return
    if dba_manual:
        blocks = _dba_manual_blocks(request, target, status_line)
    elif keep_cancel_button:
        blocks = _scheduled_blocks(request, target, status_line)
    else:
        blocks = _resolved_blocks(request, target, status_line)
    rows = db.fetch_all(
        "SELECT channel_id, message_ts FROM request_notifications WHERE request_id = %s",
        (request["id"],),
    )
    for r in rows:
        try:
            _update(client,
                channel=r["channel_id"],
                ts=r["message_ts"],
                blocks=blocks,
                text=status_line,
            )
        except Exception:
            log.exception(
                "Failed to update admin DM for request %s (channel=%s ts=%s)",
                request["id"], r["channel_id"], r["message_ts"],
            )


def dm_user_scheduled(client: WebClient, request: dict) -> None:
    """DM the requester after a scheduled approval: shows the run time, the
    server/database context, and a [Cancel] button. Records the (channel, ts)
    so the bot can chat.update this same DM when the request is cancelled or
    starts executing."""
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    sched = request["scheduled_for"]
    text = (
        f":alarm_clock: *SQL query `#{request['id']}` approved* — scheduled "
        f"for `{sched:%Y-%m-%d %H:%M UTC}`."
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            text + "\n" + request_context_md(request)}},
        _cancel_action_block(request),
    ]
    opened = client.conversations_open(users=request["requester_slack_id"])
    channel_id = opened["channel"]["id"]
    posted = _post(client,
        channel=channel_id, text=text, blocks=blocks,
        **_display_overrides(),
    )
    db.execute(
        "UPDATE requests SET requester_dm_channel_id = %s, "
        "                    requester_dm_message_ts = %s "
        "WHERE id = %s",
        (channel_id, posted["ts"], request["id"]),
    )


def update_user_scheduled_dm(
    client: WebClient,
    request: dict,
    status_line: str,
) -> None:
    """Replace the [Cancel] button on the requester's scheduled DM with a
    plain status line. Used when the request is cancelled or moves to
    executing — so the user can't click Cancel on a stale message."""
    channel = request.get("requester_dm_channel_id")
    ts = request.get("requester_dm_message_ts")
    if not channel or not ts:
        return
    sched = request.get("scheduled_for")
    sched_text = f"`{sched:%Y-%m-%d %H:%M UTC}`" if sched else "(unknown time)"
    body = (
        f"{status_line}\n"
        f"_(was scheduled for {sched_text})_\n"
        + request_context_md(request)
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    try:
        _update(client,
            channel=channel, ts=ts, text=status_line, blocks=blocks,
        )
    except Exception:
        log.exception(
            "Failed to update requester DM for request %s (channel=%s ts=%s)",
            request["id"], channel, ts,
        )


def dm_all_admins(client: WebClient, text: str) -> None:
    """Best-effort service/ops notice DM'd to every active admin — used for
    the restart 'back online' notification. Gated by bot_config
    `service_restart_dm` (default OFF, so it stays quiet during development /
    frequent deploys; set it 'on' to enable). Never raises: a failed DM must
    not hold up startup."""
    try:
        from .. import config as cfg
        if (cfg.get_setting("service_restart_dm", "off") or "").strip().lower()\
                not in {"on", "true", "yes", "1"}:
            return
    except Exception:
        return
    try:
        active = admins.list_active()
    except Exception:
        log.exception("dm_all_admins: could not list admins")
        return
    for adm in active:
        try:
            dm_requester(client, adm["slack_user_id"], text)
        except Exception:
            log.exception("service DM to admin %s failed", adm.get("slack_user_id"))


def dm_requester(client: WebClient, principal_id: str, text: str,
                 blocks: list[dict] | None = None,
                 color: str | None = None) -> str:
    """Generic DM helper. `color` is accepted for backward-compat with
    callers from the attachment-wrapped era but is now ignored —
    attachments trigger Slack's 'Show more' collapse on long
    multi-block messages, hiding the action buttons. Top-level blocks
    don't collapse, so we always render at the top level now and let
    the tier-pill emoji carry the colour cue inside the card."""
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    opened = client.conversations_open(users=principal_id)
    channel = opened["channel"]["id"]
    kwargs: dict = {"channel": channel, "text": text,
                    **_display_overrides()}
    # Always render through blocks so the trailing divider (_post) applies
    # even to plain-text DMs; `text` stays as the notification fallback.
    kwargs["blocks"] = blocks if blocks is not None else [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    ]
    res = _post(client,**kwargs)
    return res["channel"]


# ---------- requester-facing status card ----------------------------------
#
# Mirrors the admin DM's compact card layout so the two surfaces feel
# like the same product. Used by submit-confirm, approve, reject,
# changes-requested, and cancel DMs to the requester.


def requester_card_blocks(
    request: dict,
    *,
    status_emoji: str,
    status_text: str,
    body_extra: str | None = None,
    include_query: bool = True,
    with_cancel: bool = False,
) -> list[dict]:
    """Build the requester's status card. Shape:
        :emoji: *status_text* — request #142  [RO]
        `target-alias` / `db_name`
        result: CSV  (·  scheduled ...)
        (optional body_extra context line: rejection reason, etc.)
        ```query```
        [Cancel request]  (with_cancel — pending cards only)
    """
    request_id = request.get("id")
    safety = query_safety.analyze(request.get("query") or "")
    target_alias = "?"
    if request.get("target_server_id"):
        t = targets.get(request["target_server_id"])
        if t:
            target_alias = t.alias
    db_name = request.get("database_name") or "?"

    blocks: list[dict] = [
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": (f"{status_emoji} *{status_text}* — request "
                           f"`#{request_id}`    {_tier_pill(safety.main_tier)}")}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"`{target_alias}` / `{db_name}`"}},
    ]

    meta_bits = [f"result: {_fmt_result_format(request)}"]
    sched = request.get("scheduled_for")
    if sched is not None:
        sched_fallback = f"{sched:%Y-%m-%d %H:%M UTC}"
        meta_bits.append(f"scheduled: *{_slack_date(sched, sched_fallback)}*")
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn",
                      "text": "  ·  ".join(meta_bits)}],
    })

    if body_extra:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": body_extra},
        })

    if include_query:
        q = (request.get("query") or "").strip()
        if 0 < len(q) <= INLINE_QUERY_MAX_CHARS:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```\n{q}\n```"},
            })
        elif q:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn",
                              "text": f":page_facing_up: _Query is {len(q)} chars — "
                                      f"see request #{request_id} for the full text._"}],
            })

    if with_cancel:
        blocks.append(pending_actions_block(request_id))

    return blocks


def pending_actions_block(request_id: int) -> dict:
    """[✏️ Edit & resubmit] + [Cancel request] row for the requester's own
    pending card. Edit opens the pre-filled modal; the pending original is
    only superseded when the edited version is actually SUBMITTED, so
    closing the modal loses nothing. Cancel withdraws immediately. Both
    handlers re-check ownership + status atomically, so a stale button
    (request already decided) is harmless."""
    return {
        "type": "actions",
        "block_id": f"req_pending_actions_{request_id}",
        "elements": [
            {
                "type": "button",
                "action_id": ACTION_RESUBMIT,
                "text": {"type": "plain_text", "text": "✏️ Edit & resubmit"},
                "value": str(request_id),
            },
            {
                "type": "button",
                "action_id": ACTION_CANCEL_REQUEST,
                "style": "danger",
                "text": {"type": "plain_text", "text": "Cancel request"},
                "value": str(request_id),
                "confirm": {
                    "title": {"type": "plain_text", "text": "Cancel this request?"},
                    "text": {"type": "mrkdwn",
                             "text": "It will be withdrawn before any admin acts "
                                     "on it. This is final — submit a new request "
                                     "if you change your mind."},
                    "confirm": {"type": "plain_text", "text": "Yes, cancel"},
                    "deny": {"type": "plain_text", "text": "Keep it"},
                },
            },
        ],
    }


def dm_requester_card_tracked(client: WebClient, request: dict,
                              text: str, blocks: list[dict]) -> None:
    """DM the requester their status card and remember (channel, ts) in the
    request row, so the card can be chat.updated in place later (e.g. the
    [Cancel request] button replaced once the request is withdrawn)."""
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    opened = client.conversations_open(users=request["requester_slack_id"])
    channel_id = opened["channel"]["id"]
    posted = _post(client,
        channel=channel_id, text=text, blocks=blocks,
        **_display_overrides(),
    )
    db.execute(
        "UPDATE requests SET requester_dm_channel_id = %s, "
        "                    requester_dm_message_ts = %s "
        "WHERE id = %s",
        (channel_id, posted["ts"], request["id"]),
    )


def update_requester_card(client: WebClient, request: dict,
                          status_emoji: str, status_text: str,
                          body_extra: str | None = None) -> None:
    """chat.update the tracked requester card in place (button removed).
    No-op when the card was never tracked."""
    channel = request.get("requester_dm_channel_id")
    ts = request.get("requester_dm_message_ts")
    if not channel or not ts:
        return
    blocks = requester_card_blocks(
        request, status_emoji=status_emoji, status_text=status_text,
        body_extra=body_extra, with_cancel=False,
    )
    try:
        _update(client,
            channel=channel, ts=ts,
            text=f"{status_text} — request #{request['id']}",
            blocks=blocks,
        )
    except Exception:
        log.exception(
            "Failed to update requester card for request %s (channel=%s ts=%s)",
            request["id"], channel, ts,
        )


# ---------------------------------------------------------------------------
# Auto-approve FYIs (used by the shared core_submit dispatch + batch path)
# ---------------------------------------------------------------------------

_AUTO_FYI_INLINE_QUERY_MAX = 500


def query_preview_block(query: str) -> dict:
    """Section block with the query as an inline code block. Truncated
    to keep the admin DM scannable; full query lives on the request
    row in the DB for deep-dives."""
    q = (query or "").strip()
    if not q:
        q = "(empty)"
    if len(q) > _AUTO_FYI_INLINE_QUERY_MAX:
        q = q[:_AUTO_FYI_INLINE_QUERY_MAX] + "\n... (truncated)"
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"```\n{q}\n```"},
    }


def deliver_auto_approve_fyi(
    client: WebClient,
    request: dict,
    header: str,
    *,
    quiet: bool,
) -> None:
    """Deliver an auto-approve FYI (query inline, no buttons).

    Routing keeps admins informed without spamming them:
      - `quiet` (RO auto-approvals — the bulk) go to the single feed
        channel `bot_config.auto_approve_feed_channel` if set. One post,
        not one-DM-per-admin, and admins mute the channel to taste
        ("critical but silent" == Slack's own per-channel mute).
      - RW / DDL auto-approvals (`quiet=False`) always DM every admin —
        a write that skipped human approval deserves eyes now.
      - No feed channel configured, or the channel post fails → fall
        back to the per-admin DM fan-out (never lose the signal).
    """
    if not cfg.ENV.slack_enabled:  # vanilla profile: Slack is off — no-op
        return None
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        query_preview_block(request.get("query") or ""),
    ]
    overrides = display_overrides()
    feed_channel = cfg.get_setting("auto_approve_feed_channel", "") or None

    if quiet and feed_channel:
        try:
            _post(client, channel=feed_channel, text=header,
                  blocks=blocks, **overrides)
            return
        except Exception:
            log.exception(
                "auto-approve feed post to %s failed for request %s — "
                "falling back to admin DMs", feed_channel, request["id"])

    for admin in admins.list_active():
        try:
            opened = client.conversations_open(users=admin["slack_user_id"])
            _post(client,
                  channel=opened["channel"]["id"],
                  text=header,  # fallback text for notifications
                  blocks=blocks,
                  **overrides,
            )
        except Exception:
            log.exception(
                "auto-approve FYI DM failed for admin %s on request %s",
                admin["slack_user_id"], request["id"],
            )


def dm_admins_auto_approved(
    client: WebClient,
    request: dict,
    target,
    grant: dict,
) -> None:
    """FYI when an auto-approve grant short-circuits the approval flow.
    RO grants route to the quiet feed channel; RW/DDL always DM admins."""
    requester = request["requester_slack_id"]
    header = (
        f":zap: *Auto-approved* — query `#{request['id']}` "
        f"from <@{requester}> on `{target.alias}/{request['database_name']}` "
        f"({grant['max_tier'].upper()} via grant #{grant['id']})."
    )
    deliver_auto_approve_fyi(
        client, request, header, quiet=(grant.get("max_tier") == "ro"))


def dm_admins_auto_approved_fingerprint(
    client: WebClient,
    request: dict,
    target,
    fp_hit: dict,
) -> None:
    """FYI when the fingerprint cache short-circuits the approval flow — a
    repeat RO query matching a prior completed one. Always RO by
    construction, so it always routes to the quiet feed channel."""
    requester = request["requester_slack_id"]
    header = (
        f":zap: *Auto-approved (fingerprint)* — query `#{request['id']}` "
        f"from <@{requester}> on `{target.alias}/{request['database_name']}` "
        f"(RO; same shape as their completed request #{fp_hit['id']})."
    )
    deliver_auto_approve_fyi(client, request, header, quiet=True)
