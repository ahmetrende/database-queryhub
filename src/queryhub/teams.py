"""Team-based authorization (read-only).

Teams, members, and target/database grants are managed by raw SQL — see
`migrations/003_teams.sql` for the schema and INSERT examples. This module
only reads from those tables.

Authorization rules:
- Admins (rows in `admins`) bypass team checks entirely. They can see and
  submit against any enabled target on any database.
- Non-admins can only target servers granted to one of their teams.
- For a given (user, target_id), the union of `allowed_databases` arrays
  across the user's teams determines which databases are usable on that
  target. NULL/empty array on any of the user's grants means "all DBs"
  (most permissive wins, as you'd expect for additive team membership).
"""
from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

from . import access, admins, db, requesters
from . import config as cfg
from .targets import TargetServer, _row_to_target


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------
#
# Every function below has two bodies: the one that reads `teams` /
# `team_target_grants` / `user_target_grants`, and a delegation to
# `access.py`, which answers the same questions from the nine-table model.
# `bot_config.access_model_v2` chooses, and it is read per call like every
# other runtime setting, so switching back is a config change and not a deploy.
#
# The call sites are untouched — about 140 of them across ten modules — because
# a rewrite of that size is a rewrite with a missed one in it. What changes is
# what these bodies read, and nothing about what they return: the two are proved
# to agree on every (principal, target, database) answer in the fleet by
# `scripts/access_snapshot.py`, which is the only reason this flag may be
# turned on.
#
# Delete the legacy halves once the flag has been on long enough to trust —
# they are the rollback until then, which is why the duplication is deliberate
# rather than laziness.


def use_v2() -> bool:
    return (cfg.get_setting("access_model_v2", "off") or "").strip().lower() \
        == "on"


def _is_unrestricted(principal_id: str) -> bool:
    """True for admins and bypass_team_grants requesters — both ignore
    team_target_grants when authorizing (visibility + tier). The
    *visibility* layer further differentiates between the two:
        - admin: every target row in the catalog, including ts.enabled=FALSE
        - bypass: every ENABLED target (respects ts.enabled flag)
    Use `admins.is_admin(slack_user_id)` directly when you need that
    finer distinction."""
    if use_v2():
        return access.is_admin(principal_id) or \
            access.has_fleet_wide_grant(principal_id)
    return admins.is_admin(principal_id) or requesters.bypasses_team_grants(principal_id)


def list_targets_for_user(principal_id: str) -> list[TargetServer]:
    """Targets the user is allowed to query:
        - admin:  every row in target_servers (enabled + disabled), so the
                  DBA can still pick a disabled target for debug / cleanup.
        - bypass: every ENABLED target (disabled rows hidden, like a
                  normal user — bypass is "see everywhere", not "see
                  hidden things").
        - other:  only targets reached via their team grants, enabled."""
    if use_v2():
        return access.visible_targets(principal_id)
    if admins.is_admin(principal_id):
        rows = db.fetch_all(
            "SELECT id, alias, host, port, default_database, username, enabled, notes, "
            "       COALESCE(engine, 'postgres') AS engine "
            "FROM target_servers ORDER BY enabled DESC, alias"
        )
        return [_row_to_target(r) for r in rows]

    if requesters.bypasses_team_grants(principal_id):
        rows = db.fetch_all(
            "SELECT id, alias, host, port, default_database, username, enabled, notes, "
            "       COALESCE(engine, 'postgres') AS engine "
            "FROM target_servers WHERE enabled = TRUE ORDER BY alias"
        )
        return [_row_to_target(r) for r in rows]

    rows = db.fetch_all(
        "SELECT DISTINCT ts.id, ts.alias, ts.host, ts.port, ts.default_database, "
        "       ts.username, ts.enabled, ts.notes, "
        "       COALESCE(ts.engine, 'postgres') AS engine "
        "FROM target_servers ts "
        "WHERE ts.enabled = TRUE AND ( "
        "    ts.id IN (SELECT g.target_server_id FROM team_target_grants g "
        "                JOIN team_members tm ON tm.team_id = g.team_id "
        "                WHERE tm.slack_user_id = %s AND g.revoked_at IS NULL "
        "                  AND (g.expires_at IS NULL OR g.expires_at > NOW())) "
        " OR ts.id IN (SELECT target_server_id FROM user_target_grants "
        "                WHERE slack_user_id = %s AND revoked_at IS NULL "
        "                  AND (expires_at IS NULL OR expires_at > NOW()))) "
        "ORDER BY ts.alias",
        (principal_id, principal_id),
    )
    return [_row_to_target(r) for r in rows]


def search_targets_for_user(
    principal_id: str, prefix: str, limit: int = 100
) -> list[TargetServer]:
    """Same as list_targets_for_user but with alias LIKE filter for typeahead."""
    if use_v2():
        return access.search_visible_targets(principal_id, prefix, limit)
    if admins.is_admin(principal_id):
        rows = db.fetch_all(
            "SELECT id, alias, host, port, default_database, username, enabled, notes, "
            "       COALESCE(engine, 'postgres') AS engine "
            "FROM target_servers "
            "WHERE alias ILIKE %s "
            "ORDER BY enabled DESC, alias LIMIT %s",
            (f"%{prefix}%", limit),
        )
        return [_row_to_target(r) for r in rows]

    if requesters.bypasses_team_grants(principal_id):
        rows = db.fetch_all(
            "SELECT id, alias, host, port, default_database, username, enabled, notes, "
            "       COALESCE(engine, 'postgres') AS engine "
            "FROM target_servers "
            "WHERE enabled = TRUE AND alias ILIKE %s "
            "ORDER BY alias LIMIT %s",
            (f"%{prefix}%", limit),
        )
        return [_row_to_target(r) for r in rows]

    rows = db.fetch_all(
        "SELECT DISTINCT ts.id, ts.alias, ts.host, ts.port, ts.default_database, "
        "       ts.username, ts.enabled, ts.notes, "
        "       COALESCE(ts.engine, 'postgres') AS engine "
        "FROM target_servers ts "
        "WHERE ts.enabled = TRUE AND ts.alias ILIKE %s AND ( "
        "    ts.id IN (SELECT g.target_server_id FROM team_target_grants g "
        "                JOIN team_members tm ON tm.team_id = g.team_id "
        "                WHERE tm.slack_user_id = %s AND g.revoked_at IS NULL "
        "                  AND (g.expires_at IS NULL OR g.expires_at > NOW())) "
        " OR ts.id IN (SELECT target_server_id FROM user_target_grants "
        "                WHERE slack_user_id = %s AND revoked_at IS NULL "
        "                  AND (expires_at IS NULL OR expires_at > NOW()))) "
        "ORDER BY ts.alias LIMIT %s",
        (f"%{prefix}%", principal_id, principal_id, limit),
    )
    return [_row_to_target(r) for r in rows]


def can_use_target(principal_id: str, target_id: int) -> bool:
    """User may target this server if admin/bypass, OR has a team grant
    on it, OR has a user_target_grants row on it."""
    if use_v2():
        return access.can_use_target(principal_id, target_id)
    if _is_unrestricted(principal_id):
        return True
    row = db.fetch_one(
        "SELECT 1 WHERE EXISTS ("
        "    SELECT 1 FROM team_target_grants g "
        "      JOIN team_members tm ON tm.team_id = g.team_id "
        "      WHERE tm.slack_user_id = %s AND g.target_server_id = %s "
        "        AND g.revoked_at IS NULL "
        "        AND (g.expires_at IS NULL OR g.expires_at > NOW()) "
        ") OR EXISTS ("
        "    SELECT 1 FROM user_target_grants "
        "      WHERE slack_user_id = %s AND target_server_id = %s "
        "        AND revoked_at IS NULL "
        "        AND (expires_at IS NULL OR expires_at > NOW()) "
        ")",
        (principal_id, target_id, principal_id, target_id),
    )
    return row is not None


def can_use_database(principal_id: str, target_id: int, database_name: str) -> bool:
    """True iff the user's effective grant on this target permits
    `database_name`. Uses effective_grant_for_user as the single
    source of truth (user_target_grants override team grants)."""
    if use_v2():
        return access.can_use_database(principal_id, target_id, database_name)
    grant = effective_grant_for_user(principal_id, target_id)
    if grant is None:
        return False
    allowed = grant["allowed_databases"]
    return allowed is None or database_name in allowed


def allowed_databases_for_user(
    principal_id: str, target_id: int
) -> set[str] | None:
    """Return the set of database names this user may reach on the given
    target. None means 'no restriction'. Empty set means 'no grant at
    all'. Resolution mirrors effective_grant_for_user: user_target_grants
    overrides team grants entirely; team grants aggregate (NULL beats
    list)."""
    if use_v2():
        got = access.resolve_target(principal_id, target_id)
        return set() if got is None else got["databases"]
    grant = effective_grant_for_user(principal_id, target_id)
    if grant is None:
        return set()
    return grant["allowed_databases"]


_MODE_RANK = {"ro": 0, "rw": 1, "ddl": 2}


def _max_mode(modes) -> str:
    """Return the most permissive mode (ddl > rw > ro) from an iterable.
    Empty input returns 'ro'."""
    best = "ro"
    for m in modes:
        if _MODE_RANK.get(m, 0) > _MODE_RANK[best]:
            best = m
    return best


def _display_day(at):
    """`at` moved into the fleet's display timezone.

    Stored expiries are UTC and the picker writes a LOCAL end-of-day, so
    "expires 2026-08-15 01:30 Istanbul" is 2026-08-14 22:30 UTC — and reporting
    the UTC calendar date would name the day before the one the person
    experienced. Every other timestamp the UI shows is converted with this same
    `web_display_timezone`, so the refusal agreeing with them is the point.
    """
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    name = (cfg.get_setting("web_display_timezone", "UTC") or "UTC").strip() or "UTC"
    try:
        return at.astimezone(ZoneInfo(name))
    except Exception:
        return at.astimezone(timezone.utc)


def lapsed_iso(at) -> str:
    """The calendar date the holder experienced, as `YYYY-MM-DD`."""
    return _display_day(at).date().isoformat()


def fmt_lapsed(at) -> str:
    """The human form of a lapsed date. `%-d` so the 4th is "4 Aug", not "04"."""
    return _display_day(at).strftime("%-d %b %Y")


def expired_grant_at(principal_id: str, target_id: int):
    """If this principal HELD a grant here that has since lapsed, its date.

    `effective_grant_for_user` returns None for two different situations that
    read identically to the person refused: never had access, and had it until
    Thursday. The second one is the whole point of letting grants expire, and
    answering it costs one query on a path that only runs when the request is
    already being turned down.

    Returns the datetime of the LATEST expiry across their own grant and any
    team grant, since that is the one they would have been relying on, or None
    when nothing here ever applied to them. The caller formats it — a transport
    that can render a state wants the date, not a sentence.
    """
    if use_v2():
        return access.expired_grant_at(principal_id, target_id)
    row = db.fetch_one(
        "SELECT max(expires_at) AS at FROM ("
        "  SELECT g.expires_at FROM user_target_grants g "
        "   WHERE g.slack_user_id = %s AND g.target_server_id = %s "
        "     AND g.revoked_at IS NULL AND g.expires_at IS NOT NULL "
        "     AND g.expires_at <= NOW() "
        "  UNION ALL "
        "  SELECT g.expires_at FROM team_target_grants g "
        "    JOIN team_members m ON m.team_id = g.team_id "
        "   WHERE m.slack_user_id = %s AND g.target_server_id = %s "
        "     AND g.revoked_at IS NULL AND g.expires_at IS NOT NULL "
        "     AND g.expires_at <= NOW()"
        ") x",
        (principal_id, target_id, principal_id, target_id))
    return (row or {}).get("at")


def effective_grant_for_user(
    principal_id: str, target_id: int
) -> dict | None:
    """Return the effective {mode, allowed_databases, source} for a user on
    a target — None if the user has no grant of any kind on this target.
    Admins / bypass-team-grants requesters get a synthetic 'ddl' grant on
    every target with `allowed_databases=None` (full access).
    Resolution rules:
      - If user_target_grants has a row for (user, target): use that. Source = 'user'.
      - Else aggregate team_target_grants for the user on this target:
        most-permissive mode, union of allowed_databases (NULL beats list).
        Source = 'team'.

    EXPIRY (migration 096) is applied in SQL, on both levels, at RESOLUTION
    time — never by a sweep. A background job that has not run yet is a window
    in which this function answers with a grant that already ended, and this
    function is the single authority the whole product asks.

    One rule the ordering above does not make obvious: **an EXPIRED user
    override does NOT fall through to the team grants.** A user row is often
    written to NARROW what a team already allows ("this person, this target,
    read-only, until Friday"). If expiry fell through, Friday would arrive and
    silently restore the wider team access the override was written to replace
    — an expiry that INCREASES someone's access. The invariant is that expiry
    only ever removes: a dated grant that lapses leaves nothing behind.

    A REVOKED user row keeps the older fall-through behaviour, deliberately
    unchanged in this round: revocation predates expiry here and some revokes
    were issued expecting the team grant to resume. If that is wrong it is
    wrong on its own terms and should be changed knowingly, not as a side
    effect of adding expiry.
    """
    if use_v2():
        return access.legacy_shape(
            access.resolve_target(principal_id, target_id))
    if _is_unrestricted(principal_id):
        return {"mode": "ddl", "allowed_databases": None, "source": "admin_or_bypass"}

    # 1. user-level override
    # Read the row WITHOUT the expiry filter, so an EXPIRED override can be told
    # apart from an ABSENT one — the two mean different things here (see the
    # docstring: an expiry must never widen).
    u = db.fetch_one(
        "SELECT mode, allowed_databases, "
        "       (expires_at IS NOT NULL AND expires_at <= NOW()) AS expired "
        "  FROM user_target_grants "
        " WHERE slack_user_id = %s AND target_server_id = %s "
        "   AND revoked_at IS NULL",
        (principal_id, target_id),
    )
    if u is not None and u["expired"]:
        return None
    if u is not None:
        return {
            "mode": u["mode"],
            "allowed_databases": (
                set(u["allowed_databases"])
                if u["allowed_databases"] is not None and len(u["allowed_databases"]) > 0
                else None
            ),
            "source": "user",
        }

    # 2. aggregate team grants
    rows = db.fetch_all(
        "SELECT g.mode, g.allowed_databases "
        "FROM team_target_grants g "
        "JOIN team_members tm ON tm.team_id = g.team_id "
        "WHERE tm.slack_user_id = %s AND g.target_server_id = %s "
        "  AND g.revoked_at IS NULL "
        "  AND (g.expires_at IS NULL OR g.expires_at > NOW())",
        (principal_id, target_id),
    )
    if not rows:
        return None

    mode = _max_mode(r["mode"] for r in rows)
    allowed: set[str] = set()
    unrestricted = False
    for r in rows:
        dbs = r["allowed_databases"]
        if dbs is None or len(dbs) == 0:
            unrestricted = True
            break
        allowed.update(dbs)
    return {
        "mode": mode,
        "allowed_databases": None if unrestricted else allowed,
        "source": "team",
    }


def effective_grants_for_user(
    principal_id: str, target_ids: list[int]
) -> dict[int, dict | None]:
    """`effective_grant_for_user` for many targets, in a fixed number of
    queries instead of four per target.

    Same answer, same precedence, same expiry rule — a second implementation of
    an authorization rule is a second place for it to be wrong, so the two are
    compared against each other by
    `tests/test_effective_grants_batch.py::test_the_batch_agrees_with_the_single`,
    over every shape the single one distinguishes.

    Written for the admin's "what can this person reach" screen, which asked the
    single-target resolver once per target: 43 targets came to 449 round trips
    and 780ms at p95, which is a form that cannot be refreshed as someone types.
    Nothing on the SUBMISSION path uses this — that path resolves one target and
    should keep doing the cheapest possible thing.
    """
    if use_v2():
        return {tid: access.legacy_shape(got) for tid, got in
                access.resolve_many(principal_id, target_ids).items()}
    ids = list(dict.fromkeys(int(t) for t in target_ids))
    if not ids:
        return {}
    if _is_unrestricted(principal_id):
        return {tid: {"mode": "ddl", "allowed_databases": None,
                      "source": "admin_or_bypass"} for tid in ids}

    # The user overrides, expiry INCLUDED as a flag rather than filtered: an
    # expired override must return None rather than falling through to the team
    # grants, so the two cases have to stay distinguishable here exactly as they
    # are in the single-target version.
    user_rows = {
        r["target_server_id"]: r
        for r in db.fetch_all(
            "SELECT target_server_id, mode, allowed_databases, "
            "       (expires_at IS NOT NULL AND expires_at <= NOW()) AS expired "
            "  FROM user_target_grants "
            " WHERE slack_user_id = %s AND revoked_at IS NULL "
            "   AND target_server_id = ANY(%s)",
            (principal_id, ids))
    }

    team_rows: dict[int, list[dict]] = {}
    for r in db.fetch_all(
            "SELECT g.target_server_id, g.mode, g.allowed_databases "
            "  FROM team_target_grants g "
            "  JOIN team_members tm ON tm.team_id = g.team_id "
            " WHERE tm.slack_user_id = %s AND g.revoked_at IS NULL "
            "   AND (g.expires_at IS NULL OR g.expires_at > NOW()) "
            "   AND g.target_server_id = ANY(%s)",
            (principal_id, ids)):
        team_rows.setdefault(r["target_server_id"], []).append(r)

    out: dict[int, dict | None] = {}
    for tid in ids:
        u = user_rows.get(tid)
        if u is not None:
            if u["expired"]:
                out[tid] = None            # never falls through — see above
                continue
            out[tid] = {
                "mode": u["mode"],
                "allowed_databases": (
                    set(u["allowed_databases"])
                    if u["allowed_databases"] is not None
                    and len(u["allowed_databases"]) > 0
                    else None
                ),
                "source": "user",
            }
            continue
        rows = team_rows.get(tid) or []
        if not rows:
            out[tid] = None
            continue
        allowed: set[str] = set()
        unrestricted = False
        for r in rows:
            dbs = r["allowed_databases"]
            if dbs is None or len(dbs) == 0:
                unrestricted = True
                break
            allowed.update(dbs)
        out[tid] = {
            "mode": _max_mode(r["mode"] for r in rows),
            "allowed_databases": None if unrestricted else allowed,
            "source": "team",
        }
    return out


def effective_mode_for_database(
    principal_id: str, target_id: int, database_name: str
) -> str | None:
    """The most-permissive tier the user is granted FOR THIS SPECIFIC database
    on this target, or None if no grant covers this database.

    This is the tier-authorization authority — use it, NOT
    effective_grant_for_user()["mode"], to decide whether a query's required
    tier is permitted. `effective_grant_for_user` returns the max tier and the
    UNION of databases across every grant, which cross-products them: a user
    with team grants `RW on dbA` and `RO on dbB` would appear to have `RW` on
    the union `{dbA, dbB}` and could write to dbB. Here a grant
    contributes its tier ONLY to the databases it actually covers, resolved
    per grant and fail-closed."""
    if use_v2():
        got = access.resolve(principal_id, target_id, database_name)
        return got["tier"] if got else None
    if _is_unrestricted(principal_id):
        return "ddl"

    # user_target_grants overrides team grants entirely (its db list is the
    # exhaustive whitelist); it applies only if it covers this database.
    u = db.fetch_one(
        "SELECT mode, allowed_databases FROM user_target_grants "
        "WHERE slack_user_id = %s AND target_server_id = %s AND revoked_at IS NULL "
        "  AND (expires_at IS NULL OR expires_at > NOW())",
        (principal_id, target_id),
    )
    if u is not None:
        dbs = u["allowed_databases"]
        covers = dbs is None or len(dbs) == 0 or database_name in dbs
        return u["mode"] if covers else None

    # team grants: the most-permissive tier AMONG grants that cover THIS db.
    rows = db.fetch_all(
        "SELECT g.mode, g.allowed_databases "
        "FROM team_target_grants g "
        "JOIN team_members tm ON tm.team_id = g.team_id "
        "WHERE tm.slack_user_id = %s AND g.target_server_id = %s "
        "  AND g.revoked_at IS NULL "
        "  AND (g.expires_at IS NULL OR g.expires_at > NOW())",
        (principal_id, target_id),
    )
    covering = [
        r["mode"] for r in rows
        if r["allowed_databases"] is None or len(r["allowed_databases"]) == 0
        or database_name in r["allowed_databases"]
    ]
    return _max_mode(covering) if covering else None


def has_any_grant(principal_id: str) -> bool:
    """True iff the user has access via ANY mechanism: admin / bypass,
    a team membership backed by a team grant, or a user_target_grants
    row. Used at /sql entry to short-circuit users with no access at
    all (they get the access-request flow)."""
    if use_v2():
        return access.has_any_grant(principal_id)
    if _is_unrestricted(principal_id):
        return True
    row = db.fetch_one(
        "SELECT 1 WHERE EXISTS ("
        "    SELECT 1 FROM team_members tm "
        "      JOIN team_target_grants g ON g.team_id = tm.team_id "
        "      WHERE tm.slack_user_id = %s AND g.revoked_at IS NULL "
        "        AND (g.expires_at IS NULL OR g.expires_at > NOW()) "
        ") OR EXISTS ("
        "    SELECT 1 FROM user_target_grants WHERE slack_user_id = %s "
        "      AND revoked_at IS NULL "
        "      AND (expires_at IS NULL OR expires_at > NOW()) "
        ")",
        (principal_id, principal_id),
    )
    return row is not None


def list_team_summaries() -> list[dict]:
    """Every team with its live member and grant counts, for `/sql teams`.

    Flag-aware like the resolver above, and for the same reason: after the
    cutover the legacy view describes a structure nothing decides with. It is
    also the one Slack surface where the two models genuinely differ — the new
    model carries teams from an org import (`source <> 'manual'`) that the
    legacy table has never heard of.
    """
    if use_v2():
        return db.fetch_all(
            "SELECT t.id, t.name, t.description, "
            "  (SELECT count(*) FROM team_member m "
            "    WHERE m.team_id = t.id AND NOT m.is_deleted) AS member_count, "
            "  (SELECT count(*) FROM access_grant g "
            "    WHERE g.team_id = t.id AND NOT g.is_deleted "
            "      AND g.revoked_at IS NULL AND NOT g.auto_approve "
            "      AND (g.valid_until IS NULL OR g.valid_until > NOW())) "
            "    AS grant_count, "
            "  t.created_at "
            "  FROM team t WHERE NOT t.is_deleted ORDER BY t.name")
    return db.fetch_all(
        "SELECT id, name, description, member_count, grant_count, created_at "
        "  FROM v_team_summary ORDER BY name")


def team_detail(team_name: str) -> dict | None:
    """One team's members and grants, or None if there is no such team.

    `grants` are the live ones only — neither revoked nor expired. The legacy
    query listed every row in `team_target_grants`, so a grant that had been
    revoked, or had simply run out, read as access the team still had. See
    migrations 112 and 113, which fixed the same two omissions in the summary
    count, and `test_every_grant_query_checks_expiry`, which is what caught
    the expiry half here.
    """
    if use_v2():
        team = db.fetch_one(
            "SELECT id, name, description, created_at FROM team "
            " WHERE name = %s AND NOT is_deleted", (team_name,))
        if team is None:
            return None
        grants = db.fetch_all(
            "SELECT COALESCE(ts.alias, 'every target') AS alias, g.tier AS mode, "
            "       CASE WHEN g.all_databases THEN NULL "
            "            ELSE ARRAY[g.database_name] END AS allowed_databases, "
            "       g.db_role AS target_role "
            "  FROM access_grant g "
            "  LEFT JOIN target_servers ts ON ts.id = g.target_id "
            " WHERE g.team_id = %s AND NOT g.is_deleted "
            "   AND g.revoked_at IS NULL AND NOT g.auto_approve "
            "   AND (g.valid_until IS NULL OR g.valid_until > NOW()) "
            " ORDER BY alias", (team["id"],))
        members = db.fetch_all(
            "SELECT i.external_id AS slack_user_id, "
            "       COALESCE(p.display_name, '(?)') AS name "
            "  FROM team_member m "
            "  JOIN principal p ON p.id = m.principal_id "
            "  JOIN principal_identity i ON i.principal_id = p.id "
            "   AND i.provider = 'slack' AND NOT i.is_deleted "
            " WHERE m.team_id = %s AND NOT m.is_deleted "
            " ORDER BY name NULLS LAST, i.external_id", (team["id"],))
        return {"team": team, "grants": grants, "members": members}

    team = db.fetch_one(
        "SELECT id, name, description, created_at FROM teams WHERE name = %s",
        (team_name,))
    if team is None:
        return None
    grants = db.fetch_all(
        "SELECT ts.alias, g.mode, g.allowed_databases, g.target_role "
        "  FROM team_target_grants g "
        "  JOIN target_servers ts ON ts.id = g.target_server_id "
        " WHERE g.team_id = %s AND g.revoked_at IS NULL "
        "   AND (g.expires_at IS NULL OR g.expires_at > NOW()) "
        " ORDER BY ts.alias",
        (team["id"],))
    members = db.fetch_all(
        "SELECT tm.slack_user_id, "
        "       COALESCE(a.name, r.name, '(?)') AS name "
        "  FROM team_members tm "
        "  LEFT JOIN admins      a ON a.slack_user_id = tm.slack_user_id "
        "  LEFT JOIN requesters  r ON r.slack_user_id = tm.slack_user_id "
        " WHERE tm.team_id = %s "
        " ORDER BY name NULLS LAST, tm.slack_user_id", (team["id"],))
    return {"team": team, "grants": grants, "members": members}
