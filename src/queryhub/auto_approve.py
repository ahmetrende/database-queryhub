"""Auto-approval grants — time-bounded, tier-scoped exemption from
the admin approval gate.

A grant matches a request when:
  - NOW() (or a caller-supplied moment) is inside [starts_at, expires_at)
  - grant.max_tier >= required_mode

If multiple grants for the same user match, the most permissive
(highest max_tier, then latest expires_at) wins. For "is this grant
still valid at the scheduled run time?" — the submit handler calls
effective_grant(at_time=scheduled_for) and falls back to manual
approval if it returns None.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import config as cfg
from . import db

# Tier ordering — ddl is the most permissive.
_TIER_RANK = {"ro": 0, "rw": 1, "ddl": 2}


def _covers(max_tier: str, required_mode: str) -> bool:
    return _TIER_RANK[max_tier] >= _TIER_RANK[required_mode]


def grant_covers(
    grant: dict,
    required_mode: str,
    target_server_id: int | None = None,
    database_name: str | None = None,
) -> bool:
    """Pure predicate: does this grant row cover a request for
    (required_mode, target_server_id, database_name)? Scope rules:

      - tier: grant.max_tier must be >= required_mode.
      - target: a grant with target_server_id IS NULL covers every target
        (legacy/broad). A target-scoped grant matches ONLY its target — and
        only when the caller passed a matching target_server_id.
      - database: only consulted for a target-scoped grant whose
        database_name is non-NULL; then it must equal the request's db.

    Side-effect-free so it can be unit-tested without a DB."""
    if required_mode not in _TIER_RANK:
        return False
    if not _covers(grant["max_tier"], required_mode):
        return False
    g_target = grant.get("target_server_id")
    if g_target is not None and g_target != target_server_id:
        return False
    g_db = grant.get("database_name")
    if g_target is not None and g_db is not None and g_db != database_name:
        return False
    return True


def effective_grant(
    principal_id: str,
    required_mode: str,
    target_server_id: int | None = None,
    database_name: str | None = None,
    at_time: datetime | None = None,
) -> dict | None:
    """Return the auto-approve grant row that covers (user, mode, target,
    db) at `at_time` (defaults to NOW()). Multiple matches → most permissive
    (highest max_tier, then latest expires_at). A target-scoped grant only
    matches when `target_server_id` is supplied and equal; broad (NULL-scope)
    grants match any target."""
    if required_mode not in _TIER_RANK:
        return None
    at = at_time or datetime.now(timezone.utc)
    # We can't easily encode the tier-rank check in SQL portably, so we
    # filter in Python — the table is small (typically a handful of
    # active grants).
    rows = db.fetch_all(
        """
        SELECT id, slack_user_id, max_tier, target_server_id, database_name,
               starts_at, expires_at, reason, granted_by
          FROM auto_approve_grants
         WHERE slack_user_id = %s
           AND starts_at   <= %s
           AND (expires_at IS NULL OR expires_at > %s)
        """,
        (principal_id, at, at),
    )
    candidates = [
        r for r in rows
        if grant_covers(r, required_mode, target_server_id, database_name)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda r: (
            -_TIER_RANK[r["max_tier"]],
            # NULL expires_at means infinity — sort it last (most permissive).
            r["expires_at"] or datetime.max.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return candidates[0]


def list_active_grants(principal_id: str) -> list[dict]:
    """Every currently-active grant for a user (NOW() inside the
    [starts_at, expires_at) window). Used by the modal banner and by
    `/sql whoami`."""
    return db.fetch_all(
        "SELECT grant_id AS id, max_tier, starts_at, expires_at, "
        "       reason, granted_by "
        "  FROM v_active_auto_approve "
        " WHERE slack_user_id = %s "
        " ORDER BY max_tier DESC, expires_at NULLS LAST",
        (principal_id,),
    )


def best_active_tier(principal_id: str) -> tuple[str | None, datetime | None, int | None]:
    """For modal banner rendering: returns (max_tier, expires_at, grant_id)
    of the user's most permissive active grant, or (None, None, None) if
    no active grant exists.

    "Most permissive" = highest max_tier, then latest expires_at (NULL =
    never)."""
    grants = list_active_grants(principal_id)
    if not grants:
        return None, None, None
    grants.sort(
        key=lambda r: (
            _TIER_RANK[r["max_tier"]],
            r["expires_at"] or datetime.max.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    g = grants[0]
    return g["max_tier"], g.get("expires_at"), g["id"]


def fmt_until(expires_at: datetime | None) -> str:
    """Short human label for the modal banner / DM text."""
    if expires_at is None:
        return "no expiry"
    return f"until `{expires_at:%Y-%m-%d %H:%M UTC}`"


# Sentinel used as the requests.decided_by_slack_id when the auto-approve
# path fills in approval columns. Stored verbatim; the UI / audit_log
# tooling treats it as a non-Slack-user marker.
AUTO_DECIDED_BY = "AUTO"


def decided_by_name_for(grant: dict) -> str:
    """Human-readable label written to requests.decided_by_name when an
    auto-approve grant short-circuits the admin gate."""
    until = fmt_until(grant.get("expires_at"))
    return f"auto-approved (grant #{grant['id']}, max_tier={grant['max_tier']}, {until})"


# ---------------------------------------------------------------------------
# Fingerprint approval cache (Smart Routing 3b)
# ---------------------------------------------------------------------------


def fingerprint_cache_enabled() -> bool:
    val = (cfg.get_setting("fingerprint_cache_enabled", "on") or "").strip().lower()
    return val in {"on", "true", "yes", "1"}


def fingerprint_cache_hit(
    principal_id: str,
    target_server_id: int,
    database_name: str,
    fingerprint: str,
) -> dict | None:
    """Return the most recent prior request that lets this RO query skip
    admin approval, or None. A hit requires: same requester + target +
    database + query_fingerprint, a `completed` status, and completion
    within fingerprint_cache_ttl_days. CALLER must have already
    confirmed required_mode == 'ro' — this never gates writes.

    Returns a dict with the matched request id + completed_at so the
    caller can cite it in the audit log + admin FYI DM."""
    if not fingerprint or not fingerprint_cache_enabled():
        return None
    ttl_days = cfg.get_int("fingerprint_cache_ttl_days", 30)
    return db.fetch_one(
        "SELECT id, completed_at FROM requests "
        " WHERE requester_slack_id = %s "
        "   AND target_server_id   = %s "
        "   AND database_name      = %s "
        "   AND query_fingerprint  = %s "
        "   AND status = 'completed' "
        "   AND completed_at >= NOW() - make_interval(days => %s) "
        " ORDER BY completed_at DESC LIMIT 1",
        (principal_id, target_server_id, database_name, fingerprint, ttl_days),
    )


def decided_by_name_for_fingerprint(prior_request_id: int) -> str:
    """Label for requests.decided_by_name when the fingerprint cache
    short-circuits the admin gate."""
    return f"auto-approved (fingerprint match of completed request #{prior_request_id})"
