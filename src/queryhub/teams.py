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

from . import admins, db, requesters
from .targets import TargetServer, _row_to_target


def _is_unrestricted(principal_id: str) -> bool:
    """True for admins and bypass_team_grants requesters — both ignore
    team_target_grants when authorizing (visibility + tier). The
    *visibility* layer further differentiates between the two:
        - admin: every target row in the catalog, including ts.enabled=FALSE
        - bypass: every ENABLED target (respects ts.enabled flag)
    Use `admins.is_admin(slack_user_id)` directly when you need that
    finer distinction."""
    return admins.is_admin(principal_id) or requesters.bypasses_team_grants(principal_id)


def list_targets_for_user(principal_id: str) -> list[TargetServer]:
    """Targets the user is allowed to query:
        - admin:  every row in target_servers (enabled + disabled), so the
                  DBA can still pick a disabled target for debug / cleanup.
        - bypass: every ENABLED target (disabled rows hidden, like a
                  normal user — bypass is "see everywhere", not "see
                  hidden things").
        - other:  only targets reached via their team grants, enabled."""
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


def expired_grant_note(principal_id: str, target_id: int) -> str | None:
    """If this principal HELD a grant here that has since lapsed, its date.

    `effective_grant_for_user` returns None for two different situations that
    read identically to the person refused: never had access, and had it until
    Thursday. The second one is the whole point of letting grants expire, and
    answering it costs one query on a path that only runs when the request is
    already being turned down.

    Returns the LATEST expiry across their own grant and any team grant, since
    that is the one they would have been relying on. None when nothing here
    ever applied to them.
    """
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
    at = (row or {}).get("at")
    return at.strftime("%-d %b %Y") if at else None


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
