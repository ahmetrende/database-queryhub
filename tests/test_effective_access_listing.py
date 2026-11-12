"""The effective-access list names only connections a person can submit to.

Measured 2026-11-12 on a new admin: 172 rows and "Can query 172 connections",
for an admin who could reach 62. An admin resolves to every target, so the
list also held 35 deleted targets (shown as "connection disabled") and the
read replicas, which nobody submits to: a replica is reached through its
primary. The approver card said "up to RW" under rows that said DDL, with
nothing to say that the ceiling is about approving and that their own writes
go to another admin.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from queryhub import access, auto_approve, grants, teams
from queryhub.web import routes_admin as ra, routes_data

UID = "U0EXAMPLE001"
LIVE = SimpleNamespace(id=1, alias="prod-ledger", enabled=True, engine="postgres", replica_of=None)
OFF = SimpleNamespace(id=2, alias="prod-old", enabled=False, engine="postgres", replica_of=None)
GONE = SimpleNamespace(id=3, alias="prod-gone", enabled=False, engine="postgres", replica_of=None)
REPLICA = SimpleNamespace(id=4, alias="prod-ledger-read-1", enabled=True, engine="postgres",
                          replica_of=1)
CH = SimpleNamespace(id=5, alias="prod-ticks", enabled=True, engine="clickhouse", replica_of=None)
CTRL = SimpleNamespace(id=6, alias="prod-control", enabled=True, engine="postgres",
                       replica_of=None)
ADMIN_DDL = {"mode": "ddl", "allowed_databases": None, "source": "admin_or_bypass"}

SUPER = {"role": "admin", "scope_team_id": None, "all_teams": True,
         "scope_target_id": None, "all_targets": True, "max_tier": None,
         "any_tier": True, "valid_until": None}


@pytest.fixture
def admin(monkeypatch):
    st = {"roles": [dict(SUPER, max_tier="rw", any_tier=False)], "super": False,
          "asked": []}
    fleet = [LIVE, OFF, GONE, REPLICA, CH, CTRL]
    monkeypatch.setattr(ra.targets, "list_all", lambda: fleet)
    monkeypatch.setattr(ra, "_target_flags", lambda: {
        3: {"id": 3, "deleted": True, "super_admin_only": False},
        6: {"id": 6, "deleted": False, "super_admin_only": True}})

    def grants_for(pid, ids):
        st["asked"] = list(ids)
        return {i: dict(ADMIN_DDL) for i in ids}
    monkeypatch.setattr(teams, "effective_grants_for_user", grants_for)
    monkeypatch.setattr(routes_data, "_catalog_databases_map", lambda ids: {})
    monkeypatch.setattr(access, "resolve_databases", lambda pid, pairs: {})
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: [])
    monkeypatch.setattr(auto_approve, "active_grants", lambda pid, at=None: [])
    monkeypatch.setattr(access, "roles", lambda pid: st["roles"])
    monkeypatch.setattr(access, "is_super_admin", lambda pid: st["super"])
    monkeypatch.setattr(grants, "authz", lambda pid: None)
    return st


def test_a_deleted_target_and_a_replica_are_not_listed(admin):
    out, _, _ = ra._effective_access_v2(UID)
    assert sorted(r["connectionId"] for r in out) == [
        "prod-control", "prod-ledger", "prod-old", "prod-ticks"]


def test_they_are_not_even_resolved(admin):
    ra._effective_access_v2(UID)
    assert sorted(admin["asked"]) == [1, 2, 5, 6]


def test_a_disabled_target_stays_listed_and_says_so(admin):
    out, _, _ = ra._effective_access_v2(UID)
    assert next(r for r in out if r["connectionId"] == "prod-old")["enabled"] is False


def test_a_read_only_engine_row_says_so_and_shows_ro(admin):
    out, _, _ = ra._effective_access_v2(UID)
    row = next(r for r in out if r["connectionId"] == "prod-ticks")
    assert row["readOnlyEngine"] is True and row["tier"] == "RO"
    assert next(r for r in out if r["connectionId"] == "prod-ledger")["readOnlyEngine"] is False


def test_a_super_admin_only_target_is_flagged(admin):
    out, _, _ = ra._effective_access_v2(UID)
    flagged = {r["connectionId"] for r in out if r["superAdminOnly"]}
    assert flagged == {"prod-control"}


def test_an_admin_below_super_admin_approves_only_their_own_reads(admin):
    adm = ra._effective_access_v2(UID)[2]
    assert adm["role"] == "admin" and adm["selfApproves"] == "ro"
    assert adm["maxTier"] == "RW"


def test_a_super_admin_approves_all_of_their_own(admin):
    admin["roles"], admin["super"] = [SUPER], True
    adm = ra._effective_access_v2(UID)[2]
    assert adm["role"] == "admin" and adm["selfApproves"] == "all"


def test_a_scoped_approver_approves_none_of_their_own(admin):
    admin["roles"] = [{"role": "approver", "scope_team_id": None, "all_teams": True,
                       "scope_target_id": 1, "all_targets": False, "max_tier": "ro",
                       "any_tier": False, "valid_until": None}]
    adm = ra._effective_access_v2(UID)[2]
    assert adm["role"] == "approver" and adm["selfApproves"] == "none"


def test_a_row_lists_the_tiers_it_has_a_login_for(admin, monkeypatch):
    monkeypatch.setattr(ra.targets, "configured_tiers",
                        lambda ids: {i: ({"ro"} if i == 1 else {"ro", "rw", "ddl"}) for i in ids})
    out, _, _ = ra._effective_access_v2(UID)
    assert next(r for r in out if r["connectionId"] == "prod-ledger")["credentialTiers"] == ["ro"]
    assert next(r for r in out if r["connectionId"] == "prod-old")["credentialTiers"] == ["ro", "rw", "ddl"]
