"""Admin-driven access grants — the WRITE side of the /sql grant + /sql
revoke tools. (teams.py resolves grants for query auth; this module hands
them out.)

Granting access also whitelists the user: a user_target_grants row is
dormant unless the person is an enabled `requesters` row, because
requesters gates /sql entry before any grant is consulted. So grant()
upserts both in one transaction — the manual two-step that used to be
done by hand.

Authorization: an admin may grant only if they are a super-admin
(unscoped) or carry admins.can_grant, and only up to their own max_tier
(a super-admin's max_tier is NULL = unlimited). Callers must enforce
this via authz() before calling grant().
"""
from __future__ import annotations

import json
import logging

from . import db

log = logging.getLogger(__name__)

_TIER_RANK = {"ro": 0, "rw": 1, "ddl": 2}

# Fallback only — use control_plane_target_ids(). Hardcoding 1 was true of the
# author's install and of nothing else: on a fresh install target 1 is whatever
# the operator registered first, so the documented promise that "the bot's own
# control-plane DB is never grantable" held nowhere else. A DDL grant there
# means editing `audit_log` and `admins`.
CONTROL_PLANE_TARGET_ID = 1


def control_plane_target_ids() -> set[int]:
    """Target ids that can reach the bot's OWN metadata database.

    1. bot_config `control_plane_target_ids` (comma-separated) wins, for setups
       auto-detection can't see: a pooler, a CNAME, a proxy.
    2. Otherwise detect it: targets whose host+port match BOT_DB_* and whose
       default database is the metadata database. If a same-host target points
       at a different default database, it still counts — a grant there with an
       unrestricted database scope reaches the metadata DB anyway, so this
       errs toward refusing to grant.

    Returns an empty set only when there is genuinely no such target."""
    from . import config as cfg

    try:
        raw = (cfg.get_setting("control_plane_target_ids", "") or "").strip()
    except Exception:
        raw = ""
    if raw:
        explicit = {int(p.strip()) for p in raw.split(",") if p.strip().isdigit()}
        if explicit:
            return explicit

    env = cfg.ENV
    host = (getattr(env, "bot_db_host", "") or "").strip().lower()
    name = (getattr(env, "bot_db_name", "") or "").strip().lower()
    if not host or not name:
        return {CONTROL_PLANE_TARGET_ID}
    try:
        rows = db.fetch_all(
            "SELECT id, default_database FROM target_servers "
            "WHERE lower(host) = %s AND port = %s",
            (host, int(getattr(env, "bot_db_port", 5432) or 5432)),
        )
    except Exception:
        log.warning("control-plane target detection failed; falling back to "
                    "id %s", CONTROL_PLANE_TARGET_ID, exc_info=True)
        return {CONTROL_PLANE_TARGET_ID}
    exact = {r["id"] for r in rows
             if (r["default_database"] or "").strip().lower() == name}
    if exact:
        return exact
    return {r["id"] for r in rows}


def authz(principal_id: str) -> dict | None:
    """Grant capability for an admin, or None if they can't grant at all.
    Returns {'super': bool, 'max_tier': str|None}. max_tier None = unlimited.
    """
    row = db.fetch_one(
        "SELECT can_grant, max_tier, "
        "  (max_tier IS NULL AND scope_team_ids IS NULL "
        "   AND scope_target_ids IS NULL) AS is_super "
        "FROM admins WHERE slack_user_id = %s AND enabled",
        (principal_id,),
    )
    if row is None:
        return None
    if not (row["is_super"] or row["can_grant"]):
        return None
    return {"super": row["is_super"], "max_tier": row["max_tier"]}


def allowed_tiers(cap: dict) -> list[str]:
    """Tiers this granter may hand out, lowest-first."""
    if cap.get("max_tier") is None:      # super-admin / unlimited
        return ["ro", "rw", "ddl"]
    ceiling = _TIER_RANK.get(cap["max_tier"], 0)
    return [t for t, r in sorted(_TIER_RANK.items(), key=lambda kv: kv[1])
            if r <= ceiling]


def grant(
    *,
    granter_id: str,
    granter_name: str | None,
    grantee_id: str,
    grantee_profile: dict,
    target_id: int,
    mode: str,
    databases: list[str] | None,
    reason: str | None,
    notify: bool = True,
) -> dict:
    """Whitelist (upsert enabled requester) + upsert the user_target_grants
    row + audit, in one transaction. `grantee_profile` carries name / email
    / tz pulled from Slack so a freshly-whitelisted user gets a complete
    row. `databases` None/empty = all databases on the target. Returns a
    summary dict.

    When `notify` (default), the grantee gets a DM that they were granted
    access — so ANY grant path (a script, a one-off, the Slack modal)
    tells the user. The Slack /sql grant handler passes notify=False and
    sends its own combined DM (one message covering all picked targets).

    Raises PermissionError for a target that reaches the bot's own metadata
    database. That check lives HERE, not in the callers, because the Slack
    modal had it and the web admin panel did not — so the same operation was
    blocked in one UI and allowed in the other. Any future path is covered by
    construction. A grant there would let someone rewrite `audit_log` and
    `admins`, i.e. edit the record of what they did."""
    if target_id in control_plane_target_ids():
        raise PermissionError(
            "This target is the bot's own control-plane database; granting "
            "access to it would allow tampering with the audit log and the "
            "admin list.")

    name = grantee_profile.get("name")
    email = grantee_profile.get("email")
    tz = grantee_profile.get("tz")
    dbs = databases or None

    with db.transaction() as cur:
        # This path notifies on its own (notify_grantee / the modal's
        # combined DM) — keep the auth-event outbox from double-DMing.
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        cur.execute(
            "INSERT INTO requesters "
            "  (slack_user_id, email, name, enabled, added_at, added_by, tz, "
            "   bypass_team_grants) "
            "VALUES (%s, %s, %s, TRUE, NOW(), %s, %s, FALSE) "
            "ON CONFLICT (slack_user_id) DO UPDATE "
            "  SET enabled = TRUE, "
            "      email = COALESCE(requesters.email, EXCLUDED.email), "
            "      name  = COALESCE(requesters.name,  EXCLUDED.name), "
            "      tz    = COALESCE(requesters.tz,    EXCLUDED.tz) "
            "RETURNING (xmax = 0) AS inserted",
            (grantee_id, email, name, granter_id, tz),
        )
        whitelisted_now = bool(cur.fetchone()["inserted"])

        cur.execute(
            "INSERT INTO user_target_grants "
            "  (slack_user_id, target_server_id, allowed_databases, mode, "
            "   granted_at, granted_by, revoked_at) "
            "VALUES (%s, %s, %s, %s, NOW(), %s, NULL) "
            "ON CONFLICT (slack_user_id, target_server_id) DO UPDATE "
            "  SET allowed_databases = EXCLUDED.allowed_databases, "
            "      mode = EXCLUDED.mode, granted_at = NOW(), "
            "      granted_by = EXCLUDED.granted_by, revoked_at = NULL "
            "RETURNING mode, allowed_databases",
            (grantee_id, target_id, dbs, mode, granter_id),
        )
        row = cur.fetchone()

        cur.execute(
            "INSERT INTO audit_log (actor_slack_id, actor_name, action, details) "
            "VALUES (%s, %s, 'access_granted', %s::jsonb)",
            (granter_id, granter_name, json.dumps({
                "grantee": grantee_id, "target_id": target_id, "mode": mode,
                "databases": dbs, "whitelisted": whitelisted_now,
                "reason": reason,
            })),
        )
    log.info("access_granted: %s -> %s target=%s mode=%s by=%s",
             granter_id, grantee_id, target_id, mode, granter_id)
    if notify:
        from . import targets as _targets
        t = _targets.get(target_id)
        notify_grantee(grantee_id, granter_id,
                       [t.alias if t else str(target_id)],
                       row["mode"], row["allowed_databases"], whitelisted_now)
    return {"mode": row["mode"], "databases": row["allowed_databases"],
            "whitelisted_now": whitelisted_now}


def notify_grantee(grantee_id: str, granter_id: str | None,
                   aliases: list[str], mode: str,
                   databases: list[str] | None,
                   whitelisted: bool = False) -> None:
    """The ONE 'you were granted access' DM, used by every grant path so the
    wording is identical whether the grant came from /sql grant, a script,
    or anywhere else. `aliases` is one or more target aliases sharing the
    same tier + database scope (the Slack modal grants several at once).
    Best-effort and self-contained (builds its own Slack client from the bot
    token); never raises — a notification failure must not undo a committed
    grant."""
    try:
        from slack_sdk.web import WebClient

        from .config import ENV

        tlist = ", ".join(f"`{a}`" for a in aliases) if aliases else "a target"
        scope = ", ".join(databases) if databases else "all databases"
        by = f" by <@{granter_id}>" if granter_id else ""
        extra = " You're now set up in QueryHub." if whitelisted else ""
        client = WebClient(token=ENV.slack_bot_token)
        opened = client.conversations_open(users=grantee_id)
        client.chat_postMessage(
            channel=opened["channel"]["id"],
            text=(f":white_check_mark: You've been granted *{mode.upper()}* "
                  f"access to {tlist} ({scope}){by}.{extra} "
                  f"Use `/sql` to submit queries."),
        )
    except Exception:
        log.exception("grantee notification failed for %s", grantee_id)


def revoke(
    *,
    granter_id: str,
    granter_name: str | None,
    grantee_id: str,
    target_id: int,
    reason: str | None = None,
    notify: bool = True,
) -> dict | None:
    """Revoke one active user grant (sets revoked_at). Does NOT touch the
    requester row — the user may still hold access elsewhere. Returns the
    revoked row, or None if there was no active grant (already gone / raced).

    When `notify` (default), the grantee gets a DM that their access was
    revoked — so ANY revoke path (script, one-off, the Slack modal) tells
    the user, symmetric with grant()."""
    with db.transaction() as cur:
        # This path notifies on its own (notify_grantee_revoked) — keep
        # the auth-event outbox from double-DMing.
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        cur.execute(
            "UPDATE user_target_grants SET revoked_at = NOW() "
            "WHERE slack_user_id = %s AND target_server_id = %s "
            "  AND revoked_at IS NULL "
            "RETURNING slack_user_id, target_server_id, mode",
            (grantee_id, target_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "INSERT INTO audit_log (actor_slack_id, actor_name, action, details) "
            "VALUES (%s, %s, 'access_revoked', %s::jsonb)",
            (granter_id, granter_name, json.dumps({
                "grantee": grantee_id, "target_id": target_id,
                "mode": row["mode"], "reason": reason,
            })),
        )
    log.info("access_revoked: %s revoked %s on target=%s",
             granter_id, grantee_id, target_id)
    if notify:
        from . import targets as _targets
        t = _targets.get(target_id)
        notify_grantee_revoked(grantee_id, granter_id,
                               t.alias if t else str(target_id), row["mode"])
    return dict(row)


def notify_grantee_revoked(grantee_id: str, actor_id: str | None,
                           alias: str, mode: str) -> None:
    """The ONE 'your access was revoked' DM, used by every revoke path —
    symmetric with notify_grantee. Best-effort, self-contained, never
    raises (a notification failure must not undo a committed revoke)."""
    try:
        from slack_sdk.web import WebClient

        from .config import ENV

        by = f" by <@{actor_id}>" if actor_id else ""
        client = WebClient(token=ENV.slack_bot_token)
        opened = client.conversations_open(users=grantee_id)
        client.chat_postMessage(
            channel=opened["channel"]["id"],
            text=(f":lock: Your *{mode.upper()}* access to `{alias}` in "
                  f"QueryHub was revoked{by}."),
        )
    except Exception:
        log.exception("revoke notification failed for %s", grantee_id)


def list_active_grants(grantee_id: str) -> list[dict]:
    """Active per-user grants for the revoke UI (target alias + mode + dbs)."""
    return db.fetch_all(
        "SELECT g.target_server_id, ts.alias, g.mode, g.allowed_databases, "
        "       g.granted_at "
        "FROM user_target_grants g "
        "LEFT JOIN target_servers ts ON ts.id = g.target_server_id "
        "WHERE g.slack_user_id = %s AND g.revoked_at IS NULL "
        "ORDER BY ts.alias",
        (grantee_id,),
    )


def list_granted_users(typed: str = "", limit: int = 50) -> list[dict]:
    """Users who currently hold at least one active per-user grant — the
    only users worth showing in the revoke picker (NOT the whole Slack
    workspace). Optional name/id substring filter for the typeahead."""
    like = f"%{typed}%"
    return db.fetch_all(
        "SELECT g.slack_user_id, r.name, count(*) AS n_grants "
        "FROM user_target_grants g "
        "LEFT JOIN requesters r ON r.slack_user_id = g.slack_user_id "
        "WHERE g.revoked_at IS NULL "
        "  AND (%s = '' OR g.slack_user_id ILIKE %s OR r.name ILIKE %s) "
        "GROUP BY g.slack_user_id, r.name "
        "ORDER BY r.name NULLS LAST, g.slack_user_id "
        "LIMIT %s",
        (typed, like, like, limit),
    )
