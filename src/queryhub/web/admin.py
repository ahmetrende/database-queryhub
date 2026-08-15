"""Admin-panel authorization: derive the caller's admin role/scope for
`/me`, and the `require_admin` gate every `/admin/*` route runs after
`verify_session` (ADMIN_API.md auth addendum).

Reuses the Slack bot's own admin model (admins.py) — is_admin /
is_super_admin / can_approve / get_scope — so the web panel enforces the
exact same scopes as Slack, never a parallel one.
"""
from __future__ import annotations

from .. import admins, targets
from . import deps

_TIERS = ["ro", "rw", "ddl"]
_UI = {"ro": "RO", "rw": "RW", "ddl": "DDL"}


def _alias(target_id) -> str | None:
    t = targets.get(int(target_id))
    return t.alias if t else None


def admin_block(principal_id: str) -> dict | None:
    """The `admin` object /me returns for an admin caller, else None:
    {isAdmin, role: dba|super, canApprove: [tiers], connections: [aliases|"*"]}."""
    if not admins.is_admin(principal_id):
        return None
    if admins.is_super_admin(principal_id):
        return {"isAdmin": True, "role": "super",
                "canApprove": ["RO", "RW", "DDL"], "connections": ["*"]}

    # Scoped ("dba") admin: fold the permanent row + any active temp grants.
    scope = admins.get_scope(principal_id) or {}
    rows = [r for r in [scope.get("permanent"), *(scope.get("temp_grants") or [])] if r]
    unlimited_tier = False
    max_rank = -1
    star_conns = False
    conns: set[str] = set()
    for r in rows:
        mt = r.get("max_tier")
        if mt is None:
            unlimited_tier = True
        elif mt in _TIERS:
            max_rank = max(max_rank, _TIERS.index(mt))
        stids = r.get("scope_target_ids")
        if stids is None:
            star_conns = True
        else:
            for tid in stids:
                conns.add(_alias(tid) or str(tid))
    if unlimited_tier:
        can = ["RO", "RW", "DDL"]
    elif max_rank >= 0:
        can = [_UI[t] for t in _TIERS[:max_rank + 1]]
    else:
        can = ["RO"]
    return {"isAdmin": True, "role": "dba", "canApprove": can,
            "connections": ["*"] if star_conns else sorted(conns)}


def require_admin(claims: dict, need: str = "review", *, request: dict | None = None) -> str:
    """Gate for /admin/* routes. `need`='review' (any admin) or 'access'
    (super only). Pass `request` (a dict with tier + target_server_id +
    requester_slack_id — e.g. a `requests` row) on a decision to enforce
    the admin's tier/connection scope. Returns the admin's slack id."""
    uid = claims["sub"]
    if not admins.is_admin(uid):
        raise deps._error(403, "forbidden", "Admin access required.")
    if need == "access" and not admins.is_super_admin(uid):
        raise deps._error(403, "forbidden", "Super-admin access required.")
    if request is not None and not admins.can_approve(uid, request):
        raise deps._error(403, "forbidden",
                          "This query is outside your approval scope.")
    return uid
