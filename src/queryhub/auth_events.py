"""Universal authorization-change DMs, fed by the auth_event_outbox table.

Triggers (migration 060) capture every INSERT/UPDATE/DELETE on the
authorization tables — regardless of the writer, so operator psql and
scripts are covered too. This module turns pending outbox rows into
Slack DMs:

  - `build_notifications(event)` is a pure function mapping one outbox
    row to [(slack_user_id, text), ...] — unit-testable without a DB.
    Team-scoped events resolve recipients via a caller-supplied lookup.
  - `poll_loop(client, stop)` runs as a daemon thread next to the
    scheduler, draining the outbox every few seconds.

Delivery is at-least-once (a crash between send and mark can re-DM
once); rows failing repeatedly are parked with last_error after
_MAX_ATTEMPTS so a bad row can't wedge the queue.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # only a type hint — no runtime dependency on slack_sdk
    from slack_sdk import WebClient

from . import config as cfg
from . import db

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
_TIER_LABEL = {"ro": "RO (read-only)", "rw": "RW (read-write)", "ddl": "DDL"}


def is_enabled() -> bool:
    val = (cfg.get_setting("auth_event_dm_enabled", "on") or "").strip().lower()
    return val in {"on", "1", "true", "yes"}


# --------------------------------------------------------------------------
# Pure helpers — no DB access, unit-tested.
# --------------------------------------------------------------------------

def _mention(actor: str | None) -> str:
    """Render granted_by/added_by as a mention when it looks like a Slack
    id, verbatim otherwise, empty when unknown."""
    if not actor:
        return ""
    if actor[:1] in ("U", "W") and actor.isalnum() and len(actor) >= 9:
        return f" by <@{actor}>"
    return f" by {actor}"


def _fmt_dbs(dbs) -> str:
    if not dbs:
        return ""
    if isinstance(dbs, str):
        dbs = [dbs]
    quoted = ", ".join(f"`{d}`" for d in dbs)
    return f" — database(s): {quoted}"


def _fmt_until(expires_at: str | None) -> str:
    """Outbox rows carry timestamps as ISO strings (to_jsonb)."""
    if not expires_at:
        return "*permanent* (no expiry)"
    return f"until `{expires_at[:16].replace('T', ' ')} UTC`"


def _scope_phrase(target_alias: str | None, database_name: str | None) -> str:
    if target_alias and database_name:
        return f"`{target_alias}` (db `{database_name}`)"
    if target_alias:
        return f"`{target_alias}` (all databases)"
    return "*all targets*"


def build_notifications(
    event: dict,
    *,
    alias_of: "callable" = lambda tid: None,
    team_info: "callable" = lambda tid: (None, []),
) -> list[tuple[str, str]]:
    """Map one outbox event to [(slack_user_id, message)].

    `alias_of(target_server_id) -> str | None` and
    `team_info(team_id) -> (team_name | None, [member_slack_ids])` are
    injected so this stays pure/testable; the poller passes DB-backed
    versions. Returns [] for events that carry no user-visible change.
    """
    table = event["table_name"]
    op = event["op"]
    old = event.get("old_row") or {}
    new = event.get("new_row") or {}
    row = new or old
    user = event.get("slack_user_id")

    if table == "user_target_grants":
        alias = alias_of(row.get("target_server_id")) or f"target #{row.get('target_server_id')}"
        mode = (row.get("mode") or "?").upper()
        dbs = _fmt_dbs(row.get("allowed_databases"))
        if op == "INSERT":
            if new.get("revoked_at"):
                return []  # born-revoked row: historical import, nothing granted
            return [(user, f":key: Access granted: *{mode}* on `{alias}`{dbs}"
                           f"{_mention(new.get('granted_by'))}.")]
        if op == "DELETE":
            return [(user, f":no_entry: Your access to `{alias}` was removed.")]
        # UPDATE — the interesting transitions:
        if not old.get("revoked_at") and new.get("revoked_at"):
            return [(user, f":no_entry: Your access to `{alias}` was revoked.")]
        if old.get("revoked_at") and not new.get("revoked_at"):
            return [(user, f":key: Your access to `{alias}` was restored: "
                           f"*{(new.get('mode') or '?').upper()}*{_fmt_dbs(new.get('allowed_databases'))}.")]
        if new.get("revoked_at"):
            return []  # edits on an already-revoked row: invisible
        changes = []
        if old.get("mode") != new.get("mode"):
            changes.append(f"tier is now *{(new.get('mode') or '?').upper()}*")
        if old.get("allowed_databases") != new.get("allowed_databases"):
            changes.append(f"database scope is now{_fmt_dbs(new.get('allowed_databases')) or ' *all databases*'}")
        if not changes:
            return []
        return [(user, f":arrows_counterclockwise: Your access on `{alias}` changed: "
                       + "; ".join(changes) + ".")]

    if table == "auto_approve_grants":
        scope = _scope_phrase(
            alias_of(row.get("target_server_id")) if row.get("target_server_id") else None,
            row.get("database_name"),
        )
        tier = _TIER_LABEL.get(row.get("max_tier"), row.get("max_tier"))
        if op == "INSERT":
            return [(user, f":zap: Auto-approve active: up to *{tier}* on {scope}, "
                           f"{_fmt_until(new.get('expires_at'))} — matching queries dispatch "
                           f"immediately, no admin approval needed{_mention(new.get('granted_by'))}.")]
        if op == "DELETE":
            return [(user, f":zap: :x: Your auto-approve (up to *{tier}* on {scope}) was removed — "
                           "queries go through normal admin approval again.")]
        if old.get("expires_at") != new.get("expires_at"):
            return [(user, f":zap: Your auto-approve on {scope} now runs "
                           f"{_fmt_until(new.get('expires_at'))}.")]
        if old.get("max_tier") != new.get("max_tier"):
            return [(user, f":zap: Your auto-approve on {scope} is now up to *{tier}*.")]
        return []

    if table == "requesters":
        if op == "INSERT":
            if not new.get("enabled"):
                return []
            return [(user, ":white_check_mark: You've been whitelisted for QueryHub — "
                           "`/sql` is now available to you.")]
        if op == "DELETE":
            return [(user, ":no_entry: Your QueryHub whitelist entry was removed — "
                           "`/sql` is no longer available.")]
        if old.get("enabled") and not new.get("enabled"):
            return [(user, ":no_entry: Your QueryHub access was disabled.")]
        if not old.get("enabled") and new.get("enabled"):
            return [(user, ":white_check_mark: Your QueryHub access was re-enabled — "
                           "`/sql` is available again.")]
        return []

    if table == "admins":
        if op == "INSERT":
            if not new.get("enabled", True):
                return []
            return [(user, f":shield: You've been made a QueryHub *admin*"
                           f"{_mention(new.get('added_by'))}.")]
        if op == "DELETE":
            return [(user, ":shield: :x: Your QueryHub admin rights were removed.")]
        if old.get("enabled") and not new.get("enabled"):
            return [(user, ":shield: :x: Your QueryHub admin rights were disabled.")]
        if not old.get("enabled") and new.get("enabled"):
            return [(user, ":shield: Your QueryHub admin rights were re-enabled.")]
        changes = []
        if old.get("can_grant") != new.get("can_grant"):
            changes.append("you *can now grant access*" if new.get("can_grant")
                           else "you can no longer grant access")
        if old.get("max_tier") != new.get("max_tier"):
            changes.append(f"approval tier cap is now *{(new.get('max_tier') or 'unlimited').upper()}*")
        if (old.get("scope_team_ids") != new.get("scope_team_ids")
                or old.get("scope_target_ids") != new.get("scope_target_ids")):
            changes.append("your admin scope changed")
        if not changes:
            return []
        return [(user, ":shield: Your QueryHub admin rights changed: " + "; ".join(changes) + ".")]

    if table == "temp_admin_grants":
        tier = _TIER_LABEL.get(row.get("max_tier"), row.get("max_tier") or "unlimited")
        if op == "INSERT":
            return [(user, f":shield: Temporary *admin* rights granted (approve up to *{tier}*), "
                           f"{_fmt_until(new.get('expires_at'))}{_mention(new.get('granted_by'))}.")]
        if op == "DELETE" or (not old.get("revoked_at") and new.get("revoked_at")):
            return [(user, ":shield: :x: Your temporary admin rights were revoked.")]
        return []

    if table == "user_row_limit_overrides":
        if op == "INSERT":
            n = row.get("max_rows")
            return [(user, f":1234: Your queries can now return up to *{n:,}* rows, "
                           f"{_fmt_until(new.get('expires_at'))}{_mention(new.get('granted_by'))}.")]
        if op == "DELETE":
            return [(user, ":1234: :x: Your higher row limit was removed — "
                           "you're back to the normal limit.")]
        changes = []
        if old.get("max_rows") != new.get("max_rows"):
            changes.append(f"now up to *{new.get('max_rows'):,}* rows")
        if old.get("expires_at") != new.get("expires_at"):
            changes.append(f"lasts {_fmt_until(new.get('expires_at'))}")
        if not changes:
            return []
        return [(user, ":1234: Your row limit changed: " + "; ".join(changes) + ".")]

    if table == "team_target_grants":
        team_name, members = team_info(event.get("team_id"))
        team_lbl = f"`{team_name}`" if team_name else f"team #{event.get('team_id')}"
        alias = alias_of(row.get("target_server_id")) or f"target #{row.get('target_server_id')}"
        mode = (row.get("mode") or "ro").upper()
        dbs = _fmt_dbs(row.get("allowed_databases"))
        if op == "INSERT":
            text = (f":busts_in_silhouette: Your team {team_lbl} was granted "
                    f"*{mode}* on `{alias}`{dbs}.")
        elif op == "DELETE":
            text = (f":busts_in_silhouette: :no_entry: Your team {team_lbl}'s access "
                    f"to `{alias}` was removed.")
        else:
            changes = []
            if old.get("mode") != new.get("mode"):
                changes.append(f"tier is now *{(new.get('mode') or '?').upper()}*")
            if old.get("allowed_databases") != new.get("allowed_databases"):
                changes.append(f"database scope is now{_fmt_dbs(new.get('allowed_databases')) or ' *all databases*'}")
            if not changes:
                return []
            text = (f":busts_in_silhouette: Your team {team_lbl}'s access on `{alias}` "
                    "changed: " + "; ".join(changes) + ".")
        return [(m, text) for m in members]

    if table == "team_members":
        team_name, _members = team_info(event.get("team_id"))
        team_lbl = f"`{team_name}`" if team_name else f"team #{event.get('team_id')}"
        if op == "INSERT":
            return [(user, f":busts_in_silhouette: You were added to team {team_lbl} — "
                           "its target grants now apply to you (see `/sql teams`).")]
        if op == "DELETE":
            return [(user, f":busts_in_silhouette: :no_entry: You were removed from team "
                           f"{team_lbl} — its grants no longer apply to you.")]
        return []

    log.warning("auth_events: no builder for table %r — marking processed", table)
    return []


# --------------------------------------------------------------------------
# DB-backed lookups + poller
# --------------------------------------------------------------------------

def _alias_of(target_id) -> str | None:
    if target_id is None:
        return None
    from . import targets
    t = targets.get(int(target_id))
    return t.alias if t else None


def _team_info(team_id) -> tuple[str | None, list[str]]:
    if team_id is None:
        return None, []
    row = db.fetch_one("SELECT name FROM teams WHERE id = %s", (team_id,))
    members = db.fetch_all(
        "SELECT slack_user_id FROM team_members WHERE team_id = %s", (team_id,))
    return (row["name"] if row else None,
            [m["slack_user_id"] for m in members])


def process_pending(client: WebClient, limit: int = 50) -> int:
    """Drain up to `limit` outbox rows; returns how many were handled.
    Rows are locked with SKIP LOCKED so a concurrent run can't double-send
    (delivery stays at-least-once across process crashes)."""
    handled = 0
    with db.transaction() as cur:
        cur.execute(
            """SELECT id, table_name, op, slack_user_id, team_id,
                      old_row, new_row, attempts
                 FROM auth_event_outbox
                WHERE processed_at IS NULL
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED""",
            (limit,),
        )
        events = cur.fetchall()
        for ev in events:
            try:
                notes = build_notifications(
                    ev, alias_of=_alias_of, team_info=_team_info)
                for uid, text in notes:
                    if not uid:
                        continue
                    from .slack_app import notifications
                    notifications.dm_requester(client, uid, text)
                cur.execute(
                    "UPDATE auth_event_outbox "
                    "   SET processed_at = NOW(), attempts = attempts + 1 "
                    " WHERE id = %s",
                    (ev["id"],),
                )
                handled += 1
            except Exception as e:  # noqa: BLE001 — one bad row must not wedge the queue
                log.exception("auth_events: event %s failed", ev["id"])
                give_up = ev["attempts"] + 1 >= _MAX_ATTEMPTS
                cur.execute(
                    "UPDATE auth_event_outbox "
                    "   SET attempts = attempts + 1, last_error = %s, "
                    "       processed_at = CASE WHEN %s THEN NOW() END "
                    " WHERE id = %s",
                    (str(e)[:500], give_up, ev["id"]),
                )
    return handled


def poll_loop(client: WebClient, stop: threading.Event) -> None:
    """Daemon loop: drain the outbox every auth_event_poll_seconds (default
    20s; runtime-effective). Mirrors executor.scheduler_loop's shape."""
    log.info("auth-events poller started")
    while not stop.wait(timeout=cfg.get_int("auth_event_poll_seconds", 20)):
        try:
            if not is_enabled():
                continue
            n = process_pending(client)
            if n:
                log.info("auth_events: processed %d event(s)", n)
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("auth_events poll tick failed")
    log.info("auth-events poller stopped")


def _json_default(o):  # pragma: no cover — debugging aid
    return str(o)


def debug_dump(event: dict) -> str:  # pragma: no cover — operator helper
    return json.dumps(event, default=_json_default, indent=2)
