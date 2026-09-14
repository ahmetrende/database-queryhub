"""Slack UI for the schema browser: a view pushed on top of the /sql modal
that lets the user look up a table's columns/indexes without leaving their
draft query.

- `browse_cta_block()` renders the entry-point row inside the /sql modal.
- `browser_modal()` is the pushed view: a table typeahead (external_select
  fed from the bot-DB schema snapshot) plus, once a table is picked, the
  column/index detail rendered in fenced code blocks.

Data comes from `schema_catalog` (hourly snapshot); Bolt registrations
live in `handlers.py`. Like `ro_window`, this module imports no other
slack_app module to avoid cycles with `modal.py`.
"""
from __future__ import annotations

import json

from .. import schema_catalog

ACTION_OPEN = "act_open_schema_browser"    # /sql modal button → push browser
VIEW_CALLBACK = "schema_browser_modal"
B_TABLE = "blk_schema_browse_table"
A_TABLE = "act_schema_browse_table"

# Slack caps a section's text at 3000 chars; leave room for the fences.
_CHUNK_CHARS = 2800
_MAX_OPTION_TEXT = 75


def browse_cta_block() -> dict:
    """Entry-point row for the /sql modal (section + accessory button, same
    reflow-safe pattern as the RO-window CTA)."""
    return {
        "type": "section",
        "block_id": "blk_schema_browse_cta",
        "text": {"type": "mrkdwn",
                 "text": ":book: *Table and column reference* — look up a "
                         "table on the selected target without losing your "
                         "draft."},
        "accessory": {
            "type": "button",
            "action_id": ACTION_OPEN,
            "text": {"type": "plain_text", "text": "Browse schema"},
            "value": "{}",
        },
    }


def table_option(row: dict) -> dict:
    """external_select option for one schema_tables row. The option value
    carries `schema.table`; the label adds a compact size hint."""
    name = f"{row['schema_name']}.{row['table_name']}"
    hint = schema_catalog.table_summary_line(row)
    label = f"{name} — {hint}" if hint else name
    return {
        "text": {"type": "plain_text", "text": label[:_MAX_OPTION_TEXT]},
        "value": name[:_MAX_OPTION_TEXT],
    }


def _code_chunks(text: str) -> list[dict]:
    """Fenced-code section blocks, each under Slack's per-section limit,
    split on line boundaries."""
    blocks: list[dict] = []
    buf: list[str] = []
    buf_len = 0
    for line in (text or "(empty)").split("\n"):
        if buf_len + len(line) + 1 > _CHUNK_CHARS and buf:
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn", "text": "```\n" + "\n".join(buf) + "\n```"}})
            buf, buf_len = [], 0
        buf.append(line[:_CHUNK_CHARS])
        buf_len += len(line) + 1
    if buf:
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn", "text": "```\n" + "\n".join(buf) + "\n```"}})
    return blocks


def detail_blocks(trow: dict, cols: list[dict]) -> list[dict]:
    """Column/index/FK blocks for one picked table."""
    name = f"{trow['schema_name']}.{trow['table_name']}"
    summary = schema_catalog.table_summary_line(trow)
    blocks: list[dict] = [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{name}*  ·  {summary}"},
    }]
    blocks += _code_chunks(schema_catalog.format_columns(cols))
    idx_line = schema_catalog.format_indexes(trow.get("indexes"))
    if idx_line:
        blocks.append({"type": "context", "elements": [{
            "type": "mrkdwn", "text": f"*Indexes:* {idx_line}"[:2990]}]})
    fk_line = schema_catalog.format_fks(trow.get("foreign_keys"))
    if fk_line:
        blocks.append({"type": "context", "elements": [{
            "type": "mrkdwn", "text": f"*Foreign keys:* {fk_line}"[:2990]}]})
    return blocks


def browser_modal(
    *,
    target_id: int,
    target_alias: str,
    database: str,
    snapshot_ts=None,
    selected_table: str | None = None,
    body_blocks: list[dict] | None = None,
) -> dict:
    """The pushed schema-browser view. No submit button — it is a read-only
    reference; the automatic back arrow returns to the /sql modal with the
    draft intact. Target/db ride in private_metadata and are re-checked
    against the user's grants by every handler that reads them."""
    ts_label = ""
    if snapshot_ts is not None:
        ts_label = f" · snapshot {snapshot_ts:%Y-%m-%d %H:%M} UTC"
    table_element = {
        "type": "external_select",
        "action_id": A_TABLE,
        "min_query_length": 0,
        "placeholder": {"type": "plain_text", "text": "Type to search tables"},
    }
    if selected_table:
        table_element["initial_option"] = {
            "text": {"type": "plain_text",
                     "text": selected_table[:_MAX_OPTION_TEXT]},
            "value": selected_table[:_MAX_OPTION_TEXT],
        }
    blocks: list[dict] = [
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"`{target_alias}` / `{database}`{ts_label}",
            }],
        },
        {
            "type": "input",
            "block_id": B_TABLE,
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": "Table"},
            "element": table_element,
        },
    ]
    blocks += body_blocks or []
    return {
        "type": "modal",
        "callback_id": VIEW_CALLBACK,
        "title": {"type": "plain_text", "text": "Schema"},
        "close": {"type": "plain_text", "text": "Close"},
        "private_metadata": json.dumps({"t": target_id, "d": database}),
        "blocks": blocks,
    }


def info_modal(text: str) -> dict:
    """Pushed fallback when the browser can't open (no target picked yet,
    no snapshot, no access)."""
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Schema"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [{"type": "section",
                    "text": {"type": "mrkdwn", "text": text}}],
    }
