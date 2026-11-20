"""Team membership from company-SSO groups, applied at login.

An admin maps a team to one or more groups of an OIDC provider
(`team_idp_groups`, migration 222). When a person signs in through that
provider and `idp_group_sync_enabled` is on, `sync_on_login`:

- adds the person to every mapped team whose group is in the `groups` claim
  and where the person has no membership yet (`source = 'idp'`),
- removes the `source = 'idp'` memberships whose group is no longer in the
  claim, or whose mapping an admin deleted.

A `source = 'manual'` membership is never added, changed or removed here.
Groups that no team maps are ignored. All changes and their audit rows commit
in one transaction. A failure is logged and never blocks the login.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from . import audit, db
from . import config as cfg
from . import teams as teams_mod

log = logging.getLogger(__name__)

ACTION_ADDED = "team_member_idp_added"
ACTION_REMOVED = "team_member_idp_removed"
ACTOR_NAME = "idp_group_sync"

# Providers that are never a company SSO: Slack has no groups claim that we
# map, and a local account has no IdP at all.
_NOT_SSO = frozenset({"slack", "local"})
_ON = frozenset({"on", "1", "true", "yes"})


def enabled() -> bool:
    val = (cfg.get_setting("idp_group_sync_enabled", "off") or "").strip().lower()
    return val in _ON


def groups_from_claims(claims: Mapping[str, Any]) -> list[str] | None:
    """The `groups` claim as a list of strings, or None when it is absent.

    None means "the provider did not say", so the sync does nothing. An empty
    list is a real answer: the person is in no group. A claim that is not a
    list counts as absent. Non-string items are dropped. Names keep the case
    and the spaces that the IdP sent."""
    if "groups" not in claims:
        return None
    raw = claims.get("groups")
    if not isinstance(raw, list):
        return None
    return [g for g in raw if isinstance(g, str) and g]


def _is_oidc_provider(provider: str) -> bool:
    if not provider or provider in _NOT_SSO:
        return False
    from .web import auth_providers
    return provider in auth_providers.oidc_ids()


def _result(added: Sequence[int] = (), removed: Sequence[int] = (),
            skipped: str | None = None) -> dict[str, Any]:
    return {"added": list(added), "removed": list(removed), "skipped": skipped}


def sync_on_login(principal_id: str, provider: str,
                  groups: list[str] | None) -> dict[str, Any]:
    """Reconcile the `idp` memberships of one person with their SSO groups.

    Returns `{added: [team ids], removed: [team ids], skipped: reason|None}`.
    It never raises: any error is logged and returns `skipped = 'error'`."""
    try:
        # The pure checks first: a Slack login then costs no config read.
        if not provider or provider in _NOT_SSO:
            return _result(skipped="not_oidc")
        if groups is None:
            return _result(skipped="no_groups_claim")
        if not enabled():
            return _result(skipped="disabled")
        if not _is_oidc_provider(provider):
            return _result(skipped="not_oidc")
        with db.transaction() as cur:
            added, removed = _sync_in(cur, principal_id, provider, set(groups))
        if added or removed:
            log.info("idp group sync for %s via %s: added %s, removed %s",
                     principal_id, provider, added, removed)
        return _result(added, removed)
    except Exception:
        log.exception("idp group sync failed for %s via %s; the login goes on",
                      principal_id, provider)
        return _result(skipped="error")


def _sync_in(cur: Any, principal_id: str, provider: str,
             groups: set[str]) -> tuple[list[int], list[int]]:
    """The writes, on the caller's cursor. Returns (added, removed) team ids."""
    v2 = teams_mod.use_v2()
    team_table = "team t ON t.id = g.team_id AND NOT t.is_deleted" if v2 \
        else "teams t ON t.id = g.team_id"

    # 1. The teams that this provider's groups ask for.
    cur.execute(
        "SELECT g.team_id, g.group_name FROM team_idp_groups g "
        f"  JOIN {team_table} "
        " WHERE g.provider = %s", (provider,))
    desired: dict[int, str] = {}
    for r in cur.fetchall():
        if r["group_name"] in groups:
            tid = int(r["team_id"])
            # Two groups can map to one team. Record the first by name, so
            # that the row always names the same group.
            if tid not in desired or r["group_name"] < desired[tid]:
                desired[tid] = r["group_name"]

    # 2. The person's live memberships, in the live model.
    member_key: Any
    if v2:
        cur.execute(
            "SELECT principal_id FROM principal_identity "
            " WHERE provider = 'slack' AND external_id = %s AND NOT is_deleted",
            (principal_id,))
        ident = cur.fetchone()
        if ident is None:
            return [], []
        member_key = ident["principal_id"]
        cur.execute(
            "SELECT id, team_id, source, idp_group FROM team_member "
            " WHERE principal_id = %s AND NOT is_deleted", (member_key,))
    else:
        member_key = principal_id
        cur.execute(
            "SELECT team_id, source, idp_group FROM team_members "
            " WHERE slack_user_id = %s", (member_key,))
    current = cur.fetchall()
    member_teams = {int(r["team_id"]) for r in current}

    # 3. Removals: only idp rows whose team the claim no longer asks for.
    stale = [r for r in current
             if r["source"] == "idp" and int(r["team_id"]) not in desired]
    other_owner: set[tuple[int, str]] = set()
    if stale:
        # A row that another provider's mapping explains belongs to that
        # provider's next login, not to this one.
        cur.execute(
            "SELECT team_id, provider, group_name FROM team_idp_groups "
            " WHERE team_id = ANY(%s) AND provider <> %s",
            ([int(r["team_id"]) for r in stale], provider))
        other_owner = {(int(m["team_id"]), m["group_name"])
                       for m in cur.fetchall()}
    removed: list[int] = []
    for r in sorted(stale, key=lambda x: int(x["team_id"])):
        tid = int(r["team_id"])
        if (tid, r["idp_group"]) in other_owner:
            continue
        if v2:
            cur.execute(
                "UPDATE team_member SET is_deleted = TRUE, deleted_at = now(), "
                "       updated_at = now() "
                " WHERE id = %s AND source = 'idp' AND NOT is_deleted "
                "RETURNING id", (r["id"],))
        else:
            cur.execute(
                "DELETE FROM team_members "
                " WHERE team_id = %s AND slack_user_id = %s AND source = 'idp' "
                "RETURNING team_id", (tid, member_key))
        if cur.fetchone() is None:
            continue
        audit.log_in(cur, None, principal_id, ACTOR_NAME, ACTION_REMOVED,
                     {"team_id": tid, "provider": provider,
                      "group": r["idp_group"], "principal": principal_id})
        removed.append(tid)

    # 4. Adds: a mapped team where the person has no membership of any source.
    added: list[int] = []
    for tid in sorted(desired):
        if tid in member_teams:
            continue
        group = desired[tid]
        if v2:
            # `team_member_uq` is partial (WHERE NOT is_deleted). The inference
            # must carry the same predicate, or PostgreSQL raises.
            cur.execute(
                "INSERT INTO team_member (team_id, principal_id, source, idp_group) "
                "VALUES (%s, %s, 'idp', %s) "
                "ON CONFLICT (team_id, principal_id) WHERE NOT is_deleted "
                "DO NOTHING RETURNING id", (tid, member_key, group))
        else:
            cur.execute(
                "INSERT INTO team_members (team_id, slack_user_id, source, idp_group) "
                "VALUES (%s, %s, 'idp', %s) ON CONFLICT DO NOTHING "
                "RETURNING team_id", (tid, member_key, group))
        if cur.fetchone() is None:
            continue
        audit.log_in(cur, None, principal_id, ACTOR_NAME, ACTION_ADDED,
                     {"team_id": tid, "provider": provider, "group": group,
                      "principal": principal_id})
        added.append(tid)
    return added, removed
