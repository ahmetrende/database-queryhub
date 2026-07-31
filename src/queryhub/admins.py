"""Admin Slack-user registry.

Two tables back this module:

  - `admins`: permanent admin rows (no time bounds).
  - `temp_admin_grants`: time-bounded admin grants used for vacation /
    on-call deputisation. Only super-admins can issue these (a
    super-admin is a permanent admin row with all scope columns NULL
    AND enabled = TRUE).

`is_admin` / `can_approve` / `list_active` consult both tables; a user
who's in either with valid scope passes. The two tables stay separate
so the permanent admins set is immutable for audit ("who was admin on
date Y") even after temp grants expire.
"""
from __future__ import annotations

from datetime import datetime

from . import db


# ===========================================================================
# Read paths — every caller goes through these
# ===========================================================================


def list_active() -> list[dict]:
    """Every Slack user who counts as admin RIGHT NOW (permanent +
    currently-active temp). Columns: slack_user_id, name, source
    ('permanent' | 'temp'), expires_at (None for permanent). De-dup'd
    on slack_user_id with permanent rows winning."""
    rows = db.fetch_all(
        "SELECT slack_user_id, name, "
        "       'permanent'::text AS source, "
        "       NULL::timestamptz AS expires_at "
        "  FROM admins "
        " WHERE enabled = TRUE "
        "UNION ALL "
        "SELECT t.slack_user_id, "
        "       COALESCE(a.name, r.name, '(?)') AS name, "
        "       'temp'::text AS source, "
        "       t.expires_at "
        "  FROM v_active_temp_admins t "
        "  LEFT JOIN admins      a ON a.slack_user_id     = t.slack_user_id "
        "  LEFT JOIN requesters  r ON r.slack_user_id     = t.slack_user_id "
        "  WHERE NOT EXISTS ("
        "      SELECT 1 FROM admins p "
        "       WHERE p.slack_user_id = t.slack_user_id AND p.enabled = TRUE) "
        "ORDER BY name NULLS LAST"
    )
    return rows


def is_admin(principal_id: str) -> bool:
    """True iff the user has either a permanent admin row OR at least
    one active temp grant. Cheap — single query against the union."""
    row = db.fetch_one(
        "SELECT 1 "
        "  FROM admins "
        " WHERE slack_user_id = %s AND enabled = TRUE "
        "UNION ALL "
        "SELECT 1 "
        "  FROM v_active_temp_admins "
        " WHERE slack_user_id = %s "
        " LIMIT 1",
        (principal_id, principal_id),
    )
    return row is not None


def is_super_admin(principal_id: str) -> bool:
    """A super-admin is a permanent admin row with all three scope
    columns NULL — they can approve anything AND grant temp admins to
    others. Temp grants can NOT make someone a super-admin (their own
    scope columns mirror the same shape, and the grant code path
    requires the issuer to be permanent + unrestricted)."""
    row = db.fetch_one(
        "SELECT 1 FROM admins "
        " WHERE slack_user_id = %s "
        "   AND enabled       = TRUE "
        "   AND max_tier         IS NULL "
        "   AND scope_team_ids   IS NULL "
        "   AND scope_target_ids IS NULL",
        (principal_id,),
    )
    return row is not None


# ---------- mutation helpers (raw SQL also works, see OPERATIONS) ----------


def add(principal_id: str, name: str | None, added_by: str | None) -> None:
    db.execute(
        "INSERT INTO admins (slack_user_id, name, added_by) VALUES (%s, %s, %s) "
        "ON CONFLICT (slack_user_id) DO UPDATE "
        "SET enabled = TRUE, name = EXCLUDED.name",
        (principal_id, name, added_by),
    )


def disable(principal_id: str) -> None:
    db.execute(
        "UPDATE admins SET enabled = FALSE WHERE slack_user_id = %s",
        (principal_id,),
    )


# ===========================================================================
# Scope-based approval check
# ===========================================================================
#
# A request is approvable by `admin_slack_id` iff EITHER:
#   - their permanent `admins` row admits the request, OR
#   - any of their active temp grants admits it.
#
# Each candidate row carries the same shape (max_tier + scope_team_ids
# + scope_target_ids). NULL on a column = wildcard.

_TIER_RANK = {"ro": 0, "rw": 1, "ddl": 2}


def _candidate_scope_rows(admin_slack_id: str) -> list[dict]:
    """Every "admin-like" row this user has right now — permanent
    + active temp. Each row carries the three scope columns; the
    `source` column distinguishes them in audit / debug output."""
    return db.fetch_all(
        "SELECT scope_team_ids, scope_target_ids, max_tier, "
        "       'permanent' AS source, NULL::integer AS grant_id "
        "  FROM admins "
        " WHERE slack_user_id = %s AND enabled = TRUE "
        "UNION ALL "
        "SELECT scope_team_ids, scope_target_ids, max_tier, "
        "       'temp' AS source, grant_id "
        "  FROM v_active_temp_admins "
        " WHERE slack_user_id = %s",
        (admin_slack_id, admin_slack_id),
    )


def _request_tier(request: dict) -> str:
    """The engine-aware required tier (ro/rw/ddl) for a request.

    Prefer the value persisted at submit — it was classified with the
    target's engine and cannot be tampered with by the client. Otherwise
    derive it, but resolve the engine first (from the request row, else by
    looking up its target) so a non-Postgres query is never classified
    with the Postgres parser. That mis-classification is the SEC-ENG bug:
    the approval scope check used to call required_mode() with no engine,
    so a T-SQL statement the Postgres parser reads as read-only could be
    admitted below its true tier."""
    persisted = (request.get("required_tier") or "").strip().lower()
    if persisted in _TIER_RANK:
        return persisted
    from . import query_safety  # lazy to avoid cycle
    engine = request.get("engine")
    if not engine:
        tid = request.get("target_server_id")
        if tid is not None:
            from . import targets  # lazy to avoid cycle
            t = targets.get(int(tid))
            engine = t.engine if t else None
    return query_safety.required_mode(request.get("query") or "",
                                      engine=(engine or "postgres"))


def _scope_admits(scope: dict, request: dict) -> bool:
    """True iff one (scope_team_ids, scope_target_ids, max_tier) row
    admits the request. Identical semantics as the original admins
    check — just factored out so both permanent and temp rows can
    use it."""
    # max_tier
    if scope["max_tier"]:
        request_tier = _request_tier(request)
        if _TIER_RANK.get(request_tier, 99) > _TIER_RANK.get(scope["max_tier"], 99):
            return False

    # target scope. NULL means "every target" (a deliberate wildcard); an EMPTY
    # array means "no target", and must admit nothing. Testing truthiness
    # collapsed those two into the wildcard, so a scope written as `{}` — which
    # is what a typo in the documented `ARRAY[...]` recipe produces — silently
    # granted fleet-wide approval rights instead of none.
    if scope["scope_target_ids"] is not None:
        allowed_targets = scope["scope_target_ids"]
        tid = request.get("target_server_id")
        if not allowed_targets or tid is None or tid not in allowed_targets:
            return False

    # team scope — same NULL-vs-empty distinction.
    if scope["scope_team_ids"] is not None:
        allowed_teams = scope["scope_team_ids"]
        if not allowed_teams:
            return False
        requester = request.get("requester_slack_id")
        if not requester:
            return False
        teams_rows = db.fetch_all(
            "SELECT team_id FROM team_members WHERE slack_user_id = %s",
            (requester,),
        )
        requester_team_ids = {r["team_id"] for r in teams_rows}
        if not requester_team_ids.intersection(scope["scope_team_ids"]):
            return False

    return True


def can_approve(admin_slack_id: str, request: dict) -> bool:
    """True iff the admin has at least one candidate row (permanent or
    temp) that admits this request. Out-of-scope candidates don't
    veto — only the absence of any in-scope candidate does."""
    if not request:
        return False
    candidates = _candidate_scope_rows(admin_slack_id)
    if not candidates:
        return False
    return any(_scope_admits(c, request) for c in candidates)


def get_scope(admin_slack_id: str) -> dict | None:
    """Permanent admins row + any active temp grants for one user.
    Useful for audit snapshots and `/sql whoami`. Returns None when
    the user has no admin presence at all."""
    perm = db.fetch_one(
        "SELECT slack_user_id, name, email, enabled, "
        "       scope_team_ids, scope_target_ids, max_tier "
        "  FROM admins WHERE slack_user_id = %s",
        (admin_slack_id,),
    )
    temps = db.fetch_all(
        "SELECT grant_id, max_tier, scope_team_ids, scope_target_ids, "
        "       starts_at, expires_at, reason, granted_by "
        "  FROM v_active_temp_admins WHERE slack_user_id = %s",
        (admin_slack_id,),
    )
    if perm is None and not temps:
        return None
    return {"permanent": perm, "temp_grants": temps}


# ===========================================================================
# Temp admin grants — only super-admins can issue
# ===========================================================================


class NotASuperAdmin(Exception):
    """Raised by grant_temp_admin / revoke_temp_admin when the issuer
    isn't a super-admin (permanent admin with all scope cols NULL)."""


def grant_temp_admin(
    *,
    granted_by: str,
    principal_id: str,
    expires_at: datetime | None,
    max_tier: str | None = None,
    scope_team_ids: list[int] | None = None,
    scope_target_ids: list[int] | None = None,
    starts_at: datetime | None = None,
    reason: str | None = None,
) -> int:
    """Issue a temp admin grant. The issuer MUST be a super-admin —
    we re-check here even though admin management is normally raw
    SQL, so any code path that calls this helper enforces the rule.

    Returns the new grant id."""
    if not is_super_admin(granted_by):
        raise NotASuperAdmin(
            f"{granted_by} is not a super-admin (permanent admin row "
            f"with all scope columns NULL is required to issue temp grants)"
        )
    if max_tier is not None and max_tier not in _TIER_RANK:
        raise ValueError(f"max_tier must be ro / rw / ddl, got {max_tier!r}")
    row = db.fetch_one(
        "INSERT INTO temp_admin_grants "
        "  (slack_user_id, max_tier, scope_team_ids, scope_target_ids, "
        "   starts_at, expires_at, reason, granted_by) "
        "VALUES (%s, %s, %s, %s, COALESCE(%s, NOW()), %s, %s, %s) "
        "RETURNING id",
        (principal_id, max_tier, scope_team_ids, scope_target_ids,
         starts_at, expires_at, reason, granted_by),
    )
    if row is None:
        raise RuntimeError("INSERT temp_admin_grants RETURNING produced no row")
    return row["id"]


def revoke_temp_admin(*, granted_by: str, grant_id: int) -> bool:
    """Set `revoked_at = NOW()` on a temp grant. Same super-admin
    check as grant_temp_admin. Returns True if a row was updated.
    Idempotent — re-revoking is a no-op."""
    if not is_super_admin(granted_by):
        raise NotASuperAdmin(
            f"{granted_by} is not a super-admin; can't revoke temp admin"
        )
    row = db.fetch_one(
        "UPDATE temp_admin_grants "
        "   SET revoked_at = NOW() "
        " WHERE id = %s AND revoked_at IS NULL "
        "RETURNING id",
        (grant_id,),
    )
    return row is not None


def list_temp_grants(principal_id: str | None = None,
                     include_expired: bool = False) -> list[dict]:
    """All temp grants, optionally filtered by user. include_expired
    pulls revoked/expired rows too (for audit). Default returns only
    currently-active grants."""
    if include_expired:
        if principal_id:
            return db.fetch_all(
                "SELECT id, slack_user_id, max_tier, scope_team_ids, "
                "       scope_target_ids, starts_at, expires_at, "
                "       reason, granted_by, granted_at, revoked_at "
                "  FROM temp_admin_grants WHERE slack_user_id = %s "
                " ORDER BY granted_at DESC",
                (principal_id,),
            )
        return db.fetch_all(
            "SELECT id, slack_user_id, max_tier, scope_team_ids, "
            "       scope_target_ids, starts_at, expires_at, "
            "       reason, granted_by, granted_at, revoked_at "
            "  FROM temp_admin_grants ORDER BY granted_at DESC"
        )
    if principal_id:
        return db.fetch_all(
            "SELECT grant_id AS id, slack_user_id, max_tier, "
            "       scope_team_ids, scope_target_ids, starts_at, "
            "       expires_at, reason, granted_by "
            "  FROM v_active_temp_admins WHERE slack_user_id = %s "
            " ORDER BY expires_at NULLS LAST",
            (principal_id,),
        )
    return db.fetch_all(
        "SELECT grant_id AS id, slack_user_id, max_tier, "
        "       scope_team_ids, scope_target_ids, starts_at, "
        "       expires_at, reason, granted_by "
        "  FROM v_active_temp_admins ORDER BY expires_at NULLS LAST"
    )
