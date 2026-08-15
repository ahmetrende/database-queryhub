"""Helpers for multi-query batch submissions ("bundles").

A bundle groups N `requests` rows under a single `request_bundles` row.
Each item is otherwise a normal request: same status enum, same executor
path, same audit_log. Only the notification layer treats a bundle
specially (single DM per admin, summary DM to the requester once every
item is decided).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from . import config as cfg


class BundleItem(TypedDict):
    """One fully-validated item ready for INSERT. The submit handler
    builds this from the parsed modal state after running the same
    per-item validation that the single-shot path runs (mode check,
    pre-flight EXPLAIN, duplicate guard, etc.)."""
    target_server_id: int
    target_alias: str            # for audit_log details only
    database_name: str
    query: str
    wants_result: bool
    result_format: str           # 'csv' | 'xlsx' (only meaningful if wants_result=True)
    explain_plan: list | None    # jsonb; None unless RO + plan logging on
    # Optional (web submits set it; Slack submits leave it out → NULL). Read
    # via .get() at INSERT time so the Slack path needs no change.
    risk_summary: str | None


# ------------------------------- config -----------------------------------


def is_enabled() -> bool:
    val = (cfg.get_setting("batch_enabled", "off") or "off").strip().lower()
    return val in {"on", "true", "yes", "1"}


def max_items() -> int:
    """Upper bound on items per bundle. Reads from bot_config so we can
    tighten / loosen at runtime without a redeploy."""
    return max(1, cfg.get_int("batch_max_items", 5))


# ---------------------------- write helpers -------------------------------


def insert_bundle_with_items(
    cur: Any,
    *,
    requester_slack_id: str,
    requester_name: str | None,
    justification: str | None,
    scheduled_for: datetime | None,
    items: list[BundleItem],
    origin: str = "slack",
) -> dict:
    """Insert the parent bundle row and every item in one transaction.
    Caller MUST be inside `db.transaction()` — we don't manage commit /
    rollback here; that stays with the caller so the audit_log inserts
    can ride along atomically.

    Returns {bundle_id, item_rows: [...]} where each item_row is a dict
    with the columns SELECT'd at the end (id, status, etc.) so the
    caller can pass the rows directly to notifications / audit."""
    if not items:
        raise ValueError("insert_bundle_with_items: items must be non-empty")

    cur.execute(
        """
        INSERT INTO request_bundles
            (requester_slack_id, requester_name, justification, scheduled_for)
        VALUES (%s, %s, %s, %s)
        RETURNING id, created_at
        """,
        (requester_slack_id, requester_name, justification, scheduled_for),
    )
    parent = cur.fetchone()
    if parent is None:
        raise RuntimeError("INSERT request_bundles RETURNING produced no row")
    bundle_id = parent["id"]

    item_rows: list[dict] = []
    for position, item in enumerate(items, start=1):
        cur.execute(
            """
            INSERT INTO requests
                (requester_slack_id, requester_name, target_server_id,
                 database_name, query, wants_result, result_format,
                 justification, scheduled_for, explain_plan,
                 bundle_id, position, origin, risk_summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                    %s, %s)
            RETURNING id, requester_slack_id, requester_name,
                      target_server_id, database_name, query,
                      wants_result, result_format, justification, status,
                      scheduled_for, bundle_id, position, origin
            """,
            (
                requester_slack_id,
                requester_name,
                item["target_server_id"],
                item["database_name"],
                item["query"],
                item["wants_result"],
                item.get("result_format", "csv"),
                # Justification stays on the bundle; per-item column gets
                # the same text so existing single-item views still show
                # something useful when filtering by `requests` alone.
                justification,
                scheduled_for,
                None if item["explain_plan"] is None else _json(item["explain_plan"]),
                bundle_id,
                position,
                # Without origin, the result-routing gate treated web
                # batch items as slack-origin and delivered their CSV to Slack
                # anyway. Carry the bundle origin onto every item so the gate
                # honors "answer on the channel it came from".
                origin,
                item.get("risk_summary"),
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT requests RETURNING produced no row")
        item_rows.append(row)

    return {"bundle_id": bundle_id, "item_rows": item_rows}


def _json(value: Any) -> str:
    import json
    return json.dumps(value)


# ---------------------------- read helpers --------------------------------


def get_bundle(cur: Any, bundle_id: int) -> dict | None:
    cur.execute(
        "SELECT id, requester_slack_id, requester_name, justification, "
        "       scheduled_for, status, created_at "
        "  FROM request_bundles WHERE id = %s",
        (bundle_id,),
    )
    return cur.fetchone()


def list_items(cur: Any, bundle_id: int) -> list[dict]:
    """All items in a bundle, ordered by position. Returns rich rows
    (joins target_servers for alias) so the notification renderer
    doesn't have to do per-item lookups."""
    cur.execute(
        """
        SELECT r.id, r.position, r.status, r.target_server_id,
               r.database_name, r.query, r.wants_result, r.justification,
               r.row_count, r.truncated, r.error_message,
               r.executed_at, r.completed_at, r.risk_summary,
               ts.alias AS target_alias, ts.host AS target_host
          FROM requests r
          JOIN target_servers ts ON ts.id = r.target_server_id
         WHERE r.bundle_id = %s
         ORDER BY r.position
        """,
        (bundle_id,),
    )
    return cur.fetchall()


def recompute_status(cur: Any, bundle_id: int) -> str:
    """Recompute and write `request_bundles.status` from the live item
    state. Called after every item state transition (approve / reject /
    cancel / complete / fail). Idempotent.

    Rules:
      - any item in {pending, approved, scheduled, executing,
        awaiting_dba_manual, changes_requested} → bundle 'pending'
      - else if every item is 'cancelled' → bundle 'cancelled'
      - else if at least one item completed AND at least one terminal
        non-completed (rejected / failed / cancelled) → 'partial'
      - else (all completed, or all rejected, or all failed) → 'decided'
    """
    items = list_items(cur, bundle_id)
    if not items:
        return "pending"

    in_flight = {"pending", "approved", "scheduled", "executing",
                 "awaiting_dba_manual", "changes_requested"}
    statuses = [it["status"] for it in items]
    if any(s in in_flight for s in statuses):
        new_status = "pending"
    elif all(s == "cancelled" for s in statuses):
        new_status = "cancelled"
    elif "completed" in statuses and any(
            s in {"rejected", "failed", "cancelled"} for s in statuses):
        new_status = "partial"
    else:
        new_status = "decided"

    cur.execute(
        "UPDATE request_bundles SET status = %s WHERE id = %s AND status <> %s",
        (new_status, bundle_id, new_status),
    )
    return new_status
