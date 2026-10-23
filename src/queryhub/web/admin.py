"""Admin-panel authorization: derive the caller's admin role/scope for
`/me`, and the `require_admin` gate every `/admin/*` route runs after
`verify_session` (ADMIN_API.md auth addendum).

Reuses the Slack bot's own admin model (admins.py) — is_admin /
is_super_admin / can_approve / get_scope — so the web panel enforces the
exact same scopes as Slack, never a parallel one.
"""
from __future__ import annotations

from .. import admins, targets
from .. import config as cfg
from . import deps

_TIERS = ["ro", "rw", "ddl"]
_UI = {"ro": "RO", "rw": "RW", "ddl": "DDL"}


def _alias(target_id) -> str | None:
    t = targets.get(int(target_id))
    return t.alias if t else None


def admin_block(principal_id: str) -> dict | None:
    """The `admin` object /me returns for an admin caller, else None:
    {isAdmin, isDba, role: dba|super, canApprove: [tiers], connections: [aliases|"*"]}.

    `role` is `dba` for every admin who is not a super-admin, a temporary or a
    scoped one too. `isDba` is the answer to `require_admin(claims, "dba")`:
    true for a super-admin and for a permanent, unscoped admin (PLA-1592 U4a)."""
    if not admins.is_admin(principal_id):
        return None
    if admins.is_super_admin(principal_id):
        return {"isAdmin": True, "isDba": True, "role": "super",
                "canApprove": ["RO", "RW", "DDL"], "connections": ["*"]}

    # Scoped ("dba") admin: fold the permanent row + any active temp grants.
    # A permanent row that is disabled, or whose end has passed (migration
    # 147), grants no scope, so it adds none to the fold. Folding it would
    # show a scope that no decision honours.
    scope = admins.get_scope(principal_id) or {}
    perm = scope.get("permanent")
    if perm and (perm.get("enabled") is False or perm.get("ended")):
        perm = None
    rows = [r for r in [perm, *(scope.get("temp_grants") or [])] if r]
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
    return {"isAdmin": True, "isDba": admins.is_dba(principal_id), "role": "dba",
            "canApprove": can,
            "connections": ["*"] if star_conns else sorted(conns)}


def require_sync_principal(claims: dict) -> str:
    """Gate for the IDP panel's machine routes: the reconcile and the
    notification outbox. Nothing else calls them.

    Not `require_admin`. Under the access model an admin role is fleet-wide,
    so with no tier cap the key that signs for the panel's cron would also
    have approved anything; the gate asked for more authority than the job
    needs. The caller must be the principal named in `idp_sync_principal`
    and must have arrived through an identity assertion — a browser session
    for the same account is refused. That account can be a DISABLED
    requesters row with no grants: it passes here and nowhere else
    (docs/AUTH.md §1.2).
    """
    uid = claims.get("sub") or ""
    expected = (cfg.get_setting("idp_sync_principal", "") or "").strip()
    if not expected or uid != expected or claims.get("provider") != "idp":
        raise deps._error(403, "forbidden",
                          "Refused: you are not the sync principal.")
    return uid


# The levels `require_admin` knows, from the widest to the narrowest.
LEVELS = ("review", "dba", "access")


def require_admin(claims: dict, need: str = "review", *, request: dict | None = None) -> str:
    """Gate for /admin/* routes. `need` is one of three levels:

    - 'review': any admin, including a temporary or a scoped one.
    - 'dba': a PERMANENT (no end date) and UNSCOPED (all teams, all targets)
      admin role row (`admins.is_dba`). A tier ceiling is allowed, so a
      super-admin passes too. A temporary admin, a scoped admin and an
      approver do not pass.
    - 'access': a super-admin only.

    An unknown level raises ValueError, so a typo fails closed instead of
    falling back to 'review'. Pass `request` (a dict with tier +
    target_server_id + requester_slack_id — e.g. a `requests` row) on a
    decision to enforce the admin's tier/connection scope. Returns the
    admin's slack id."""
    if need not in LEVELS:
        raise ValueError(f"unknown admin level {need!r}")
    uid = claims["sub"]
    if not admins.is_admin(uid):
        raise deps._error(403, "forbidden", "Refused: you need admin access.")
    if need == "access" and not admins.is_super_admin(uid):
        raise deps._error(403, "forbidden",
                          "Refused: you need super-admin access.")
    # A super-admin is a DBA by definition. The super-admin check goes first,
    # so a super-admin costs no extra read.
    if need == "dba" and not (admins.is_super_admin(uid) or admins.is_dba(uid)):
        raise deps._error(403, "forbidden", "Refused: you need DBA access.")
    if request is not None and not admins.can_approve(uid, request):
        raise deps._error(403, "forbidden",
                          "Refused: this query is outside your approval scope.")
    return uid
