"""Slash sub-commands for `/sql <subcmd>`.

When the user types `/sql` with no text, the modal opens (legacy
behavior, handled in handlers.handle_slash_sql). When text is present,
dispatch() routes to the appropriate sub-command handler defined here.

All output goes via Slack ephemeral message (respond callback) — only
the invoker sees the result, no channel noise. Tables use fenced code
blocks for column alignment.

Admin-only subcommands check admins.is_admin first.
"""
from __future__ import annotations

import difflib
import logging
from datetime import timezone

from slack_sdk.web import WebClient

from .. import admins, bundles, csv_import, db, grants, schema_catalog, teams, templates
from .. import config as cfg
from .. import targets as targets_mod
from . import admin_grant, modal, notifications

log = logging.getLogger(__name__)


def dispatch(text: str, user_id: str, client: WebClient, respond,
             body: dict | None = None) -> None:
    """Route `/sql <text>` to a sub-command. Unknown sub-commands get
    a friendly "did you mean" hint + pointer to /sql help.

    `body` is the raw Slack slash-command payload; sub-commands that
    need to open a modal pull `trigger_id` from it. Most sub-commands
    ignore it (they just print to `respond`)."""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        # Shouldn't reach here — caller checks text non-empty
        _respond(respond, "Usage: `/sql help` for available commands.")
        return
    name = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    for cmd, fn, admin_only, _desc in _SUBCOMMANDS:
        if cmd == name:
            if admin_only and not admins.is_admin(user_id):
                _respond(respond,
                         f":no_entry: `/sql {name}` is admin-only.")
                return
            try:
                fn(user_id, rest, client, respond, body or {})
            except Exception:
                log.exception("sub-command /sql %s failed", name)
                _respond(respond, f":x: `/sql {name}` failed unexpectedly. "
                                  f"Check the bot logs.")
            return

    # Unknown.
    known = [c[0] for c in _SUBCOMMANDS]
    close = difflib.get_close_matches(name, known, n=1, cutoff=0.5)
    hint = f"Did you mean `/sql {close[0]}`? " if close else ""
    _respond(respond,
             f":question: Unknown sub-command `/sql {name}`. "
             f"{hint}Run `/sql help` for the full list.")


# ---------- helpers ----------

def _respond(respond, text: str) -> None:
    respond({"response_type": "ephemeral", "text": text})


def _code_block(lines: list[str]) -> str:
    return "```\n" + "\n".join(lines) + "\n```"


def _trunc(s: str, w: int) -> str:
    """Truncate a string to width w, padding right with spaces."""
    s = s or ""
    if len(s) > w:
        return s[: w - 1] + "…"
    return s.ljust(w)


def _fmt_grants_summary(row: dict) -> str:
    """Compact 'what this user can do' string for the roles table."""
    bits = []
    if row["is_admin"]:
        tier = row["admin_max_tier"] or "super"
        scope_bits = []
        if row["admin_scope_team_ids"]:
            scope_bits.append(f"team-scoped({len(row['admin_scope_team_ids'])})")
        if row["admin_scope_target_ids"]:
            scope_bits.append(f"target-scoped({len(row['admin_scope_target_ids'])})")
        scope = (" " + ",".join(scope_bits)) if scope_bits else ""
        bits.append(f"admin({tier}){scope}")
    if row["is_bypass"]:
        bits.append("bypass")
    if row["teams"]:
        bits.append(f"team={','.join(row['teams'])}")
    if row["user_grants"]:
        bits.append(",".join(row["user_grants"]))
    return " · ".join(bits) if bits else "—"


# ---------- /sql help ----------

def _handle_help(user_id, rest, client, respond, body):
    is_admin = admins.is_admin(user_id)
    lines = []
    for cmd, _fn, admin_only, desc in _SUBCOMMANDS:
        if admin_only and not is_admin:
            continue
        flag = " (admin)" if admin_only else ""
        lines.append(f"  /sql {cmd:<10s} {desc}{flag}")
    lines.append("")
    lines.append("  /sql            Open the SQL request modal")
    _respond(respond, "*Available commands:*\n" + _code_block(lines))


# ---------- /sql whoami ----------

def _handle_whoami(user_id, rest, client, respond, body):
    row = db.fetch_one(
        "SELECT * FROM p_metrics_who_can_what WHERE slack_user_id = %s",
        (user_id,),
    )
    # Pull any active temp admin grants — these don't yet flow through
    # p_metrics_who_can_what, so query the view directly. Multiple grants
    # per user possible (we show all so the user sees expiry per grant).
    temp_grants = db.fetch_all(
        "SELECT grant_id, max_tier, scope_team_ids, scope_target_ids, "
        "       starts_at, expires_at, granted_by "
        "  FROM v_active_temp_admins "
        " WHERE slack_user_id = %s "
        " ORDER BY expires_at NULLS LAST",
        (user_id,),
    )

    if row is None and not temp_grants:
        _respond(respond,
                 ":information_source: You're not in the bot's user "
                 "list yet. Contact the DBA team to be added.")
        return

    # row may be NULL but temp_grants set — synthesise a minimal display row.
    if row is None:
        row = {
            "slack_user_id": user_id, "name": "(?)", "email": None,
            "is_admin": False, "is_bypass": False,
            "admin_max_tier": None, "admin_scope_team_ids": None,
            "admin_scope_target_ids": None,
            "teams": [], "user_grants": [],
        }

    lines = [
        f"Slack ID    : {row['slack_user_id']}",
        f"Name        : {row['name']}",
        f"Email       : {row['email'] or '-'}",
        f"Admin       : {'yes' if row['is_admin'] else 'no'}",
    ]
    if row["is_admin"]:
        lines.append(f"  max_tier         : {row['admin_max_tier'] or '(any)'}")
        lines.append(f"  scope_team_ids   : {row['admin_scope_team_ids'] or '(any)'}")
        lines.append(f"  scope_target_ids : {row['admin_scope_target_ids'] or '(any)'}")
    if temp_grants:
        lines.append(f"Temp admin  : yes ({len(temp_grants)} active grant(s))")
        for g in temp_grants:
            until = (f"until {g['expires_at']:%Y-%m-%d %H:%M UTC}"
                     if g["expires_at"] else "no expiry")
            tier = g["max_tier"] or "any"
            lines.append(
                f"  grant #{g['grant_id']:<4d} tier={tier:<3s} {until}"
                f"  (by {g['granted_by']})"
            )
    lines.append(f"Bypass      : {'yes' if row['is_bypass'] else 'no'}")
    lines.append(f"Teams       : {', '.join(row['teams']) if row['teams'] else '-'}")
    lines.append(f"User grants : {', '.join(row['user_grants']) if row['user_grants'] else '-'}")
    _respond(respond, "*Your roles + grants:*\n" + _code_block(lines))


# ---------- /sql history ----------

def _handle_history(user_id, rest, client, respond, body):
    rows = db.fetch_all(
        "SELECT r.id, r.status, r.created_at, r.executed_at, r.completed_at, "
        "       r.row_count, r.truncated, r.error_message, "
        "       (SELECT alias FROM target_servers WHERE id = r.target_server_id) AS target, "
        "       r.database_name "
        "  FROM requests r "
        " WHERE r.requester_slack_id = %s AND r.status <> 'draft' "
        " ORDER BY r.id DESC LIMIT 10",
        (user_id,),
    )
    if not rows:
        _respond(respond, "_You have no requests yet. Run `/sql` to submit one._")
        return

    from ..executor import _fmt_count, _fmt_duration

    header = (
        f"{_trunc('ID', 5)} "
        f"{_trunc('STATUS', 12)} "
        f"{_trunc('TARGET', 24)} "
        f"{_trunc('DB', 20)} "
        f"{_trunc('ROWS', 8)} "
        f"{_trunc('DUR', 7)} "
        f"CREATED"
    )
    lines = [header]
    for r in rows:
        # Row count: only meaningful once executed and not failed.
        if r["row_count"] is not None:
            rc = _fmt_count(r["row_count"])
            if r["truncated"]:
                rc += "+"  # signals "capped at max_rows, more existed"
        else:
            rc = "—"

        # Duration: prefer executed_at → completed_at; fall back to
        # created_at → completed_at if executed_at isn't populated
        # (rare — happens if execution failed at the targets.get step
        # before _run's UPDATE).
        if r["executed_at"] and r["completed_at"]:
            elapsed = (r["completed_at"] - r["executed_at"]).total_seconds()
            dur = _fmt_duration(elapsed)
        else:
            dur = "—"

        ts = r["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"{_trunc('#'+str(r['id']), 5)} "
            f"{_trunc(r['status'], 12)} "
            f"{_trunc(r['target'] or '?', 24)} "
            f"{_trunc(r['database_name'] or '?', 20)} "
            f"{_trunc(rc, 8)} "
            f"{_trunc(dur, 7)} "
            f"{ts}"
        )
        # Failed: append the scrubbed error message as a continuation
        # line so the user can see what went wrong inline.
        if r["status"] == "failed" and r["error_message"]:
            err = r["error_message"].replace("\n", " ").strip()
            if len(err) > 90:
                err = err[:89] + "…"
            lines.append(f"        └ {err}")

    _respond(respond, "*Your last 10 requests:*\n" + _code_block(lines))


# ---------- /sql roles (admin) ----------

def _handle_roles(user_id, rest, client, respond, body):
    rows = db.fetch_all("SELECT * FROM p_metrics_who_can_what")
    if not rows:
        _respond(respond, "_No users in the system._")
        return
    header = f"{_trunc('NAME', 22)} {_trunc('SLACK_ID', 13)} ROLE / GRANTS"
    lines = [header]
    for r in rows:
        lines.append(
            f"{_trunc(r['name'], 22)} "
            f"{_trunc(r['slack_user_id'], 13)} "
            f"{_fmt_grants_summary(r)}"
        )
    _respond(respond, f"*All users ({len(rows)}):*\n" + _code_block(lines))


# ---------- /sql pending (admin) ----------

def _handle_pending(user_id, rest, client, respond, body):
    rows = db.fetch_all(
        "SELECT r.id, r.status, r.created_at, r.query, "
        "       r.requester_slack_id, r.requester_name, "
        "       (SELECT alias FROM target_servers WHERE id = r.target_server_id) AS target "
        "  FROM requests r "
        " WHERE r.status IN ('pending','approved','scheduled','executing','awaiting_dba_manual','changes_requested') "
        " ORDER BY (r.status = 'pending') DESC, r.created_at"   # pending first, then oldest
    )
    if not rows:
        _respond(respond, ":sparkles: _No in-flight requests right now._")
        return

    from datetime import datetime
    now = datetime.now(timezone.utc)

    def _age(dt) -> str:
        s = (now - dt).total_seconds()
        if s < 60:    return f"{int(s)}s"
        if s < 3600:  return f"{int(s // 60)}m"
        if s < 86400: return f"{int(s // 3600)}h"
        return f"{int(s // 86400)}d"

    # Slack caps a message at 50 blocks; each request renders as up to 3
    # (section + actions + divider), so cap the detailed list.
    MAX_DETAIL = 15
    blocks: list[dict] = [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*In-flight requests ({len(rows)})* — pending first."},
    }]
    for i, r in enumerate(rows):
        if i >= MAX_DETAIL:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                "text": f"_…and {len(rows) - MAX_DETAIL} more not shown._"}]})
            break
        name = r["requester_name"] or r["requester_slack_id"]
        q = " ".join((r["query"] or "").split())        # collapse whitespace to one line
        preview = (q[:100] + "…") if len(q) > 100 else q
        text = (
            f"*#{r['id']}* · `{r['status']}` · _{name}_ → `{r['target'] or '?'}`\n"
            f"_submitted {r['created_at']:%Y-%m-%d %H:%M UTC} · {_age(r['created_at'])} ago_"
        )
        if preview:
            text += f"\n```{preview}```"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        # Approve / Reject buttons reuse the existing notification handlers
        # (they read request_id from the button value), so acting from here
        # decides the request + updates its original admin DM. Pending only.
        if r["status"] == "pending":
            blocks.append({
                "type": "actions",
                "block_id": f"pending_act_{r['id']}",
                "elements": [
                    {"type": "button", "style": "primary",
                     "action_id": notifications.ACTION_APPROVE,
                     "text": {"type": "plain_text", "text": "Approve"}, "value": str(r["id"])},
                    {"type": "button", "style": "danger",
                     "action_id": notifications.ACTION_REJECT,
                     "text": {"type": "plain_text", "text": "Reject"}, "value": str(r["id"])},
                ],
            })
        blocks.append({"type": "divider"})

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "_Acting decides the request and notifies the requester. Re-run `/sql pending` to refresh this list._"}]})
    respond({"response_type": "ephemeral",
             "text": f"In-flight requests ({len(rows)})", "blocks": blocks})


# ---------- /sql kill (admin) ----------

def _handle_kill(user_id, rest, client, respond, body):
    arg = rest.strip().lower()
    current = (db.fetch_one("SELECT value FROM bot_config WHERE key = 'kill_switch'") or {}).get("value", "off")
    if not arg:
        _respond(respond,
                 f"Current state: `kill_switch = {current}`\n"
                 f"Usage: `/sql kill on` or `/sql kill off`")
        return
    if arg not in ("on", "off"):
        _respond(respond, ":warning: Usage: `/sql kill on` or `/sql kill off`.")
        return
    db.execute("UPDATE bot_config SET value = %s WHERE key = 'kill_switch'", (arg,))
    cfg.invalidate_cache()   # the kill switch must take effect on the next call
    user_name = db.fetch_one(
        "SELECT name FROM admins WHERE slack_user_id = %s", (user_id,)
    )
    actor = (user_name or {}).get("name") or user_id
    log.warning("kill_switch toggled to %s by %s", arg, actor)
    _respond(respond,
             f":zap: `kill_switch` set to *{arg}* by {actor}. "
             f"(was: `{current}`)")


# ---------- /sql teams ----------

def _handle_teams(user_id, rest, client, respond, body):
    """Two shapes:
      /sql teams                  → all teams with member + grant counts
      /sql teams <name>           → drill into one team: members + grants
    """
    arg = rest.strip().split()[0] if rest.strip() else ""
    if arg:
        _handle_one_team(arg, respond)
        return

    rows = db.fetch_all(
        "SELECT id, name, description, member_count, grant_count "
        "  FROM v_team_summary ORDER BY name"
    )
    if not rows:
        _respond(respond, "_No teams defined yet._")
        return
    header = (f"{_trunc('TEAM', 22)} {_trunc('MEMBERS', 9)} "
              f"{_trunc('GRANTS', 8)} DESCRIPTION")
    lines = [header]
    for r in rows:
        lines.append(
            f"{_trunc(r['name'], 22)} "
            f"{_trunc(str(r['member_count']), 9)} "
            f"{_trunc(str(r['grant_count']), 8)} "
            f"{(r['description'] or '—')[:60]}"
        )
    _respond(respond,
             f"*Teams ({len(rows)}):*\n" + _code_block(lines)
             + "\nDrill into one: `/sql teams <name>`")


def _handle_one_team(team_name, respond):
    team = db.fetch_one(
        "SELECT id, name, description, created_at "
        "  FROM teams WHERE name = %s",
        (team_name,),
    )
    if team is None:
        _respond(respond,
                 f":question: No team named `{team_name}`. "
                 "Run `/sql teams` for the full list.")
        return
    grants = db.fetch_all(
        "SELECT ts.alias, g.mode, g.allowed_databases, g.target_role "
        "  FROM team_target_grants g "
        "  JOIN target_servers ts ON ts.id = g.target_server_id "
        " WHERE g.team_id = %s ORDER BY ts.alias",
        (team["id"],),
    )
    members = db.fetch_all(
        "SELECT tm.slack_user_id, "
        "       COALESCE(a.name, r.name, '(?)') AS name "
        "  FROM team_members tm "
        "  LEFT JOIN admins      a ON a.slack_user_id     = tm.slack_user_id "
        "  LEFT JOIN requesters  r ON r.slack_user_id     = tm.slack_user_id "
        " WHERE tm.team_id = %s "
        " ORDER BY name NULLS LAST, tm.slack_user_id",
        (team["id"],),
    )

    lines = [
        f"Team        : {team['name']}",
        f"Description : {team['description'] or '—'}",
        f"Created     : {team['created_at']:%Y-%m-%d}",
        "",
    ]
    lines.append(f"Members ({len(members)}):")
    if members:
        for m in members:
            lines.append(f"  • {_trunc(m['name'], 24)} {m['slack_user_id']}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"Grants ({len(grants)}):")
    if grants:
        for g in grants:
            dbs = "all DBs" if not g["allowed_databases"] else\
                  f"DBs: {', '.join(g['allowed_databases'][:5])}" +\
                  (f" (+{len(g['allowed_databases']) - 5} more)"
                   if len(g["allowed_databases"]) > 5 else "")
            role_bit = f" role={g['target_role']}" if g["target_role"] else ""
            lines.append(
                f"  • {_trunc(g['alias'], 28)} [{g['mode'].upper():>3}] "
                f"{dbs}{role_bit}"
            )
    else:
        lines.append("  (none)")

    _respond(respond, f"*Team `{team['name']}`:*\n" + _code_block(lines))


# ---------- /sql templates ----------

def _handle_templates(user_id, rest, client, respond, body):
    """Management surface for saved templates. To USE a template, pick
    it from the dropdown at the top of the /sql modal. This subcommand
    handles the rarer operations:

        /sql templates                       → list own + shared
        /sql templates delete <name>         → drop (owner only)
        /sql templates share <name>          → flip is_shared TRUE
        /sql templates unshare <name>        → flip is_shared FALSE
    """
    parts = rest.strip().split(maxsplit=1)
    head = parts[0].lower() if parts else ""
    tail = parts[1].strip() if len(parts) > 1 else ""

    if not head:
        _list_templates(user_id, respond)
        return
    if head in ("delete", "rm", "remove"):
        if not tail:
            _respond(respond, "Usage: `/sql templates delete <name>`")
            return
        ok = templates.delete(user_id, tail)
        if ok:
            _respond(respond, f":wastebasket: Template `{tail}` deleted.")
        else:
            _respond(respond,
                     f":question: No template `{tail}` owned by you. "
                     f"Shared templates can only be deleted by their owner.")
        return
    if head in ("share", "unshare"):
        if not tail:
            _respond(respond, f"Usage: `/sql templates {head} <name>`")
            return
        is_shared = (head == "share")
        ok = templates.set_shared(user_id, tail, is_shared)
        if ok:
            verb = "shared with the workspace" if is_shared else "made personal"
            _respond(respond, f":lock_with_ink_pen: Template `{tail}` {verb}.")
        else:
            _respond(respond,
                     f":question: No template `{tail}` owned by you. "
                     f"Only the owner can change the share flag.")
        return

    # Anything else (e.g. someone typing `/sql templates foo`) → point
    # them at the modal, which is the primary way to use a template now.
    _respond(respond,
             f":bulb: To use a template, run `/sql` and pick "
             f"`{rest.strip()}` from the *Template* dropdown at the top "
             f"of the modal. Subcommand actions: "
             f"`delete <name>`, `share <name>`, `unshare <name>`.")


def _list_templates(user_id, respond):
    data = templates.list_for(user_id)
    own = data["own"]
    shared = data["shared"]
    if not own and not shared:
        _respond(respond,
                 "_You have no saved templates yet. Run `/sql`, fill the "
                 "form, type a name into *Save as template* before "
                 "submitting — the query gets saved alongside the run._")
        return

    sections = []
    if own:
        header = (f"{_trunc('NAME', 24)} {_trunc('TARGET', 26)} "
                  f"{_trunc('DB', 18)} {_trunc('USES', 5)} SHARED")
        lines = [header]
        for r in own:
            shared_mark = "yes" if r["is_shared"] else "no"
            lines.append(
                f"{_trunc(r['name'], 24)} "
                f"{_trunc(r['target_alias'] or '—', 26)} "
                f"{_trunc(r['database_name'] or '—', 18)} "
                f"{_trunc(str(r['use_count']), 5)} "
                f"{shared_mark}"
            )
        sections.append(f"*Your templates ({len(own)}):*\n" + _code_block(lines))

    if shared:
        header = (f"{_trunc('NAME', 22)} {_trunc('OWNER', 13)} "
                  f"{_trunc('TARGET', 26)} {_trunc('DB', 18)} USES")
        lines = [header]
        for r in shared:
            lines.append(
                f"{_trunc(r['name'], 22)} "
                f"{_trunc(r['owner_slack_id'], 13)} "
                f"{_trunc(r['target_alias'] or '—', 26)} "
                f"{_trunc(r['database_name'] or '—', 18)} "
                f"{r['use_count']}"
            )
        sections.append(f"*Shared templates ({len(shared)}):*\n" + _code_block(lines))

    sections.append("Run a template: `/sql templates <name>`")
    sections.append("Manage: `/sql templates delete <name>` · `/sql templates share <name>`")
    _respond(respond, "\n\n".join(sections))


# ---------- /sql batch ----------

def _handle_batch(user_id, rest, client, respond, body):
    """Open the batch modal. Gated by bot_config.batch_enabled so we
    can ship the wiring dark and flip it on after smoke-testing."""
    if not bundles.is_enabled():
        _respond(respond,
                 ":information_source: Batch mode is currently disabled. "
                 "Use `/sql` to submit a single query.")
        return
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        _respond(respond, ":x: Could not open the batch modal (missing trigger_id).")
        return
    try:
        client.views_open(
            trigger_id=trigger_id,
            view=modal.build_batch_modal(
                item_count=1,
                max_items=bundles.max_items(),
                principal_id=user_id,
            ),
        )
    except Exception:
        log.exception("views_open failed for /sql batch")
        _respond(respond, ":x: Could not open the batch modal — check the bot logs.")


def _handle_import(user_id, rest, client, respond, body):
    """Open the CSV import modal. Gated by bot_config.csv_import_enabled
    and the per-user import grant."""
    if not csv_import.is_enabled():
        _respond(respond, ":information_source: CSV import is currently disabled.")
        return
    if not csv_import.can_import(user_id):
        _respond(respond,
                 ":no_entry: You don't have import permission. "
                 "Contact the DBA team to be granted CSV import access.")
        return
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        _respond(respond, ":x: Could not open the import modal (missing trigger_id).")
        return
    try:
        client.views_open(trigger_id=trigger_id, view=modal.build_import_modal())
    except Exception:
        log.exception("views_open failed for /sql import")
        _respond(respond, ":x: Could not open the import modal — check the bot logs.")


# ---------- schema catalog (tables / schema / findcol) ----------


def _allowed_schema_targets(user_id: str) -> list:
    """Targets whose schema this user may browse: admins see every enabled
    target, everyone else the targets they hold a grant on."""
    if admins.is_admin(user_id):
        return targets_mod.list_enabled()
    return teams.list_targets_for_user(user_id)


def _split_target_db(token: str) -> tuple[str, str | None]:
    """`target/db` → (target, db); bare `target` → (target, None)."""
    if "/" in token:
        target_part, database = token.split("/", 1)
        return target_part, (database or None)
    return token, None


def _resolve_target(user_id: str, name: str):
    """Match `name` against the user's browsable targets: exact alias
    first, then unique substring. Returns (target, error_text)."""
    allowed = _allowed_schema_targets(user_id)
    exact = [t for t in allowed if t.alias == name]
    if exact:
        return exact[0], None
    subs = [t for t in allowed if name.lower() in t.alias.lower()]
    if len(subs) == 1:
        return subs[0], None
    if len(subs) > 1:
        opts = ", ".join(f"`{t.alias}`" for t in subs[:8])
        return None, f"Ambiguous target `{name}` — did you mean: {opts}?"
    return None, (f"No target `{name}` among the ones you can browse. "
                  f"`/sql whoami` shows your grants.")


def _resolve_database(user_id: str, target,
                      database: str | None) -> tuple[str | None, str | None]:
    """Default to the target's default database; verify the user's grant covers
    it AND that a snapshot exists. Returns (database, error_text).

    The grant check is the part that was missing. `_resolve_target` filters by
    TARGET grant, so a user with a grant on the target could browse the schema
    of a database that grant does not include. Reproduced on this deployment
    2026-07-30 against a real restricted grant: `teams.can_use_database` said
    False for a database outside the grant, and this function returned it
    anyway. Table and column names are not row data, but they are the map of
    somebody else's system — and the web UI already gates the same view by
    database (`routes_data._granted_db`), so the two transports disagreed about
    who may see what.

    `teams.can_use_database` is the same helper the submit path uses, so there
    is one rule rather than a second copy that can drift from it.
    """
    database = database or target.default_database
    if database and not teams.can_use_database(user_id, target.id, database):
        allowed = teams.allowed_databases_for_user(user_id, target.id)
        if allowed:
            sample = ", ".join(f"`{d}`" for d in sorted(allowed)[:8])
            more = "" if len(allowed) <= 8 else f" (+{len(allowed) - 8} more)"
            return None, (f"Your grant on `{target.alias}` does not include "
                          f"database `{database}`. Allowed: {sample}{more}.")
        return None, (f"Your grant on `{target.alias}` does not include "
                      f"database `{database}`. `/sql whoami` shows your grants.")
    snapshotted = schema_catalog.list_snapshot_databases(target.id)
    if database in snapshotted:
        return database, None
    if snapshotted:
        return None, (f"No schema snapshot for `{target.alias}/{database}`. "
                      f"Snapshotted databases: "
                      + ", ".join(f"`{d}`" for d in snapshotted))
    return None, (f"No schema snapshot for `{target.alias}` yet — the "
                  f"hourly catalog job hasn't covered it (or its RO "
                  f"credential is missing).")


def _snapshot_note(target_id: int, database: str) -> str:
    ts = schema_catalog.snapshot_info(target_id, database)
    if ts is None:
        return ""
    return f"_snapshot {ts.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC_"


def _handle_tables(user_id, rest, client, respond, body):
    """`/sql tables <target>[/<db>] [pattern]` — list tables from the
    schema snapshot (partitions collapsed into their parent)."""
    parts = rest.strip().split(maxsplit=1)
    if not parts:
        _respond(respond, "Usage: `/sql tables <target>[/<db>] [pattern]`")
        return
    target_part, database = _split_target_db(parts[0])
    pattern = parts[1].strip() if len(parts) > 1 else ""
    target, err = _resolve_target(user_id, target_part)
    if err:
        _respond(respond, err)
        return
    database, err = _resolve_database(user_id, target, database)
    if err:
        _respond(respond, err)
        return
    rows = schema_catalog.search_tables(target.id, database, pattern, limit=40)
    if not rows:
        _respond(respond,
                 f"No tables matching `{pattern}` in `{target.alias}/{database}`.")
        return
    name_w = min(max(len(f"{r['schema_name']}.{r['table_name']}") for r in rows), 44)
    lines = [
        f"{_trunc(r['schema_name'] + '.' + r['table_name'], name_w):<{name_w}}  "
        f"{schema_catalog.table_summary_line(r)}"
        for r in rows
    ]
    head = (f"*{target.alias}/{database}* — {len(rows)} table(s)"
            + (f" matching `{pattern}`" if pattern else "")
            + (" (top 40 by size)" if len(rows) == 40 else ""))
    _respond(respond, f"{head}\n{_code_block(lines)}\n"
                      f"{_snapshot_note(target.id, database)}")


def _handle_schema(user_id, rest, client, respond, body):
    """`/sql schema <target>[/<db>] <table>` — columns, indexes and FKs
    for one table, from the schema snapshot."""
    parts = rest.strip().split(maxsplit=1)
    if len(parts) < 2:
        _respond(respond, "Usage: `/sql schema <target>[/<db>] <table>`")
        return
    target_part, database = _split_target_db(parts[0])
    table_ref = parts[1].strip()
    target, err = _resolve_target(user_id, target_part)
    if err:
        _respond(respond, err)
        return
    database, err = _resolve_database(user_id, target, database)
    if err:
        _respond(respond, err)
        return
    res = schema_catalog.get_table(target.id, database, table_ref)
    if res is None:
        hits = schema_catalog.search_tables(target.id, database, table_ref, limit=8)
        hint = ""
        if hits:
            hint = ("\nClose matches: "
                    + ", ".join(f"`{r['schema_name']}.{r['table_name']}`"
                                for r in hits))
        _respond(respond,
                 f"No table `{table_ref}` in `{target.alias}/{database}`.{hint}")
        return
    if isinstance(res, list):
        opts = ", ".join(f"`{r['schema_name']}.{r['table_name']}`" for r in res)
        _respond(respond,
                 f"`{table_ref}` exists in more than one schema: {opts}. "
                 f"Use the qualified name.")
        return
    trow, cols = res
    name = f"{trow['schema_name']}.{trow['table_name']}"
    out = [f"*{name}*  ·  {schema_catalog.table_summary_line(trow)}",
           _code_block(schema_catalog.format_columns(cols).split("\n"))]
    idx_line = schema_catalog.format_indexes(trow.get("indexes"))
    if idx_line:
        out.append(f"*Indexes:* {idx_line}"[:2900])
    fk_line = schema_catalog.format_fks(trow.get("foreign_keys"))
    if fk_line:
        out.append(f"*Foreign keys:* {fk_line}"[:2900])
    out.append(_snapshot_note(target.id, database))
    _respond(respond, "\n".join(out))


def _handle_findcol(user_id, rest, client, respond, body):
    """`/sql findcol <pattern>` — search column names across every target
    the user can browse. The one thing a per-DB IDE can't do."""
    pattern = rest.strip()
    if not pattern:
        _respond(respond, "Usage: `/sql findcol <column-name-pattern>`")
        return
    allowed = _allowed_schema_targets(user_id)
    if not allowed:
        _respond(respond, "You don't have access to any targets yet.")
        return
    by_id = {t.id: t for t in allowed}
    rows = schema_catalog.find_column(pattern, list(by_id), limit=40)
    if not rows:
        _respond(respond,
                 f"No columns matching `{pattern}` across your "
                 f"{len(by_id)} target(s).")
        return
    lines = []
    for r in rows:
        alias = by_id[r["target_server_id"]].alias
        loc = f"{alias}/{r['database_name']}"
        tbl = f"{r['schema_name']}.{r['table_name']}.{r['column_name']}"
        lines.append(f"{_trunc(loc, 30):<30}  {_trunc(tbl, 52):<52}  {r['data_type']}")
    head = (f"*{len(rows)} column(s)* matching `{pattern}` across "
            f"{len(by_id)} target(s)"
            + (" (top 40 by table size)" if len(rows) == 40 else ""))
    _respond(respond, f"{head}\n{_code_block(lines)}")


# NO DM PREFILL, and this is a platform rule rather than a missing feature.
#
# `/sql grant` is usually typed in the conversation where the access was asked
# for, so opening the modal with that person already picked is the obvious
# thing to want. It cannot be done: a DM between two PEOPLE is a conversation
# the bot is not a member of, and a bot token may only read conversations it
# belongs to. `conversations.info` on that channel answers `channel_not_found`
# — measured 2026-08-31 — and no scope changes that; `im:read` covers the
# bot's own DMs only. The slash-command payload carries `channel_id` and
# `channel_name: "directmessage"`, and nothing that names the other person.
#
# The way to get this is a MESSAGE SHORTCUT ("Grant QueryHub access" on the
# message that asked for it): that payload does carry `message.user`, works in
# a DM the app is not in, and hands over the request text as the reason. It
# needs the shortcut registered on the Slack app before any code here is worth
# writing.


def _handle_grant(user_id, rest, client, respond, body):
    """Open the access-grant modal. Admin-gated by the registry; finer
    `can_grant` capability is re-checked here (a plain admin without
    can_grant / super-admin gets a clear no)."""
    cap = grants.authz(user_id)
    if cap is None:
        _respond(respond,
                 ":no_entry: You're not allowed to grant access. Ask a "
                 "super-admin to enable `can_grant` for you.")
        return
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        _respond(respond, ":x: Could not open the grant modal (missing trigger_id).")
        return
    try:
        client.views_open(
            trigger_id=trigger_id,
            view=admin_grant.grant_modal(
                allowed_tiers=grants.allowed_tiers(cap)),
        )
    except Exception:
        log.exception("views_open failed for /sql grant")
        _respond(respond, ":x: Could not open the grant modal — check the bot logs.")


def _handle_revoke(user_id, rest, client, respond, body):
    """Open the revoke modal (pick a user → see + remove their grants)."""
    if grants.authz(user_id) is None:
        _respond(respond, ":no_entry: You're not allowed to revoke access.")
        return
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        _respond(respond, ":x: Could not open the revoke modal (missing trigger_id).")
        return
    try:
        client.views_open(trigger_id=trigger_id, view=admin_grant.revoke_modal())
    except Exception:
        log.exception("views_open failed for /sql revoke")
        _respond(respond, ":x: Could not open the revoke modal — check the bot logs.")


# ---------- registry ----------

# (name, handler, admin_only, description)
_SUBCOMMANDS: list[tuple[str, callable, bool, str]] = [
    ("help",    _handle_help,    False, "Show this command list"),
    ("whoami",  _handle_whoami,  False, "Your roles + grants"),
    ("history", _handle_history, False, "Your last 10 requests"),
    ("teams",   _handle_teams,   False, "List teams (or `/sql teams <name>` to drill in)"),
    ("templates", _handle_templates, False, "Manage saved templates (list / delete / share). Pick one to USE via the modal's Template dropdown."),
    ("batch",   _handle_batch,   False, "Submit up to N queries in one approval round"),
    ("import",  _handle_import,  False, "Bulk-load a CSV into the dba schema (requires import permission)"),
    ("tables",  _handle_tables,  False, "List tables on a target: `/sql tables <target>[/<db>] [pattern]`"),
    ("schema",  _handle_schema,  False, "Columns + indexes of a table: `/sql schema <target>[/<db>] <table>`"),
    ("findcol", _handle_findcol, False, "Search column names across all your targets: `/sql findcol <pattern>`"),
    ("grant",   _handle_grant,   True,  "Grant a user access to a target (pick user / RDS / tier)"),
    ("revoke",  _handle_revoke,  True,  "Revoke a user's access to a target"),
    ("roles",   _handle_roles,   True,  "All users' roles + grants"),
    ("pending", _handle_pending, True,  "In-flight request queue"),
    ("kill",    _handle_kill,    True,  "Toggle the master kill switch"),
]
