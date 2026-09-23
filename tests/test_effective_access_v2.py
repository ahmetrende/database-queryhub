"""Effective access under the new model: per database, with its provenance.

The person view answered per connection with the highest tier across the
databases it named, so RW on one database and RO on another read as RW on
both -- the one distinction a pod split exists to make. It took the granting
team and the expiry from the retired team tables, the waivers from a table
that cannot hold a team's, and approver standing from the legacy `admins` row,
which a pod lead does not have. The team view is new: what a team holds and
who signs off for it.
"""
import inspect
from types import SimpleNamespace

import pytest

from queryhub import access, auto_approve, grants, teams
from queryhub.web import routes_admin as ra, routes_data

UID = "U0EXAMPLE001"
T53 = SimpleNamespace(id=53, alias="prod-ledger", enabled=True)
T60 = SimpleNamespace(id=60, alias="prod-orders", enabled=True)


def _res(tier, source="principal", unrestricted=False):
    return {"tier": tier, "auto_tier": None, "source": source,
            "unrestricted": unrestricted, "db_role": None}


@pytest.fixture
def person(monkeypatch):
    st = {"grants": {53: {"mode": "rw", "allowed_databases": {"ledger", "audit"}, "source": "user"}},
          "decided": {(53, "ledger"): (_res("rw"), {"team_id": None, "valid_until": None}),
                      (53, "audit"): (_res("ro", "team"), {"team_id": 7, "valid_until": None})},
          "waivers": [], "reach": True, "roles": []}
    monkeypatch.setattr(ra.targets, "list_all", lambda: [T53, T60])
    monkeypatch.setattr(teams, "effective_grants_for_user", lambda pid, ids: st["grants"])
    monkeypatch.setattr(routes_data, "_catalog_databases_map", lambda ids: {60: ["orders"]})
    monkeypatch.setattr(access, "resolve_databases",
                        lambda pid, pairs: {p: st["decided"].get(p, (None, None)) for p in pairs})
    monkeypatch.setattr(ra.db, "fetch_all",
                        lambda sql, params=None: [{"id": 7, "name": "Team Alpha"}])
    monkeypatch.setattr(auto_approve, "active_grants", lambda pid, at=None: st["waivers"])
    monkeypatch.setattr(auto_approve, "_team_waiver_applies", lambda pid, t, d: st["reach"])
    monkeypatch.setattr(access, "roles", lambda pid: st["roles"])
    monkeypatch.setattr(access, "is_super_admin", lambda pid: False)
    monkeypatch.setattr(grants, "authz", lambda pid: None)
    monkeypatch.setattr(ra, "_alias_of", lambda tid: {53: "prod-ledger", 60: "prod-orders"}.get(tid))
    return st


def test_a_person_is_answered_per_database(person):
    out, _, _ = ra._effective_access_v2(UID)
    [row] = out
    assert row["mixedTiers"] is True
    assert row["tier"] == "RW"                       # the highest, as the panel reads it
    assert {(d["database"], d["tier"]) for d in row["perDatabase"]} == \
        {("ledger", "RW"), ("audit", "RO")}


def test_the_granting_team_is_named_from_the_deciding_row(person):
    out, _, _ = ra._effective_access_v2(UID)
    audit_db = next(d for d in out[0]["perDatabase"] if d["database"] == "audit")
    assert audit_db["source"] == "team" and audit_db["sourceTeam"] == "Team Alpha"


def test_every_database_on_the_server_is_asked_when_the_grant_names_none(person):
    person["grants"] = {60: {"mode": "ro", "allowed_databases": None, "source": "user"}}
    person["decided"] = {(60, "orders"): (_res("ro"), None)}
    [row] = ra._effective_access_v2(UID)[0]
    assert row["allDatabases"] is True
    assert [d["database"] for d in row["perDatabase"]] == ["orders"]


def test_an_admin_is_not_split_into_databases(person):
    person["grants"] = {53: {"mode": "ddl", "allowed_databases": None, "source": "admin_or_bypass"}}
    [row] = ra._effective_access_v2(UID)[0]
    assert row["source"] == "admin_or_bypass" and row["perDatabase"] == []


def test_a_team_waiver_is_listed_only_where_it_reaches(person):
    w = {"target_server_id": 53, "database_name": "ledger", "max_tier": "ro",
         "expires_at": None, "team_id": 7, "team_name": "Team Alpha"}
    person["waivers"] = [w]
    assert ra._effective_access_v2(UID)[1][0]["viaTeam"] == "Team Alpha"
    person["reach"] = False
    assert ra._effective_access_v2(UID)[1] == []


def test_approver_standing_comes_from_the_role_model(person):
    person["roles"] = [
        {"role": "approver", "scope_team_id": 7, "all_teams": False, "scope_target_id": 53,
         "all_targets": False, "max_tier": "ro", "any_tier": False, "valid_until": None},
        {"role": "approver", "scope_team_id": 7, "all_teams": False, "scope_target_id": 60,
         "all_targets": False, "max_tier": "rw", "any_tier": False, "valid_until": None}]
    adm = ra._effective_access_v2(UID)[2]
    assert adm["maxTier"] == "RW"
    assert adm["scopeTargets"] == ["prod-ledger", "prod-orders"]
    assert adm["scopeTeams"] == ["Team Alpha"]
    assert adm["scopeTargetsAll"] is False


def test_no_role_means_no_approver_standing(person):
    assert ra._effective_access_v2(UID)[2] is None


def test_the_route_picks_the_model_by_the_switch():
    src = inspect.getsource(ra.admin_effective_access)
    assert "_effective_access_v2(slack_id)" in src and "_effective_access_legacy(slack_id)" in src


# --- the batched resolver ----------------------------------------------------

def test_resolve_databases_filters_rows_the_way_covering_does(monkeypatch):
    rows = [
        {"target_id": 53, "mine": True, "tier": "rw", "rank": 2, "auto_approve": False,
         "merge_with_team": False, "all_targets": False, "all_databases": False,
         "database_name": "ledger", "db_role": None, "team_id": None, "valid_until": None,
         "expired": False, "not_started": False},
        {"target_id": 53, "mine": True, "tier": "ro", "rank": 1, "auto_approve": False,
         "merge_with_team": False, "all_targets": False, "all_databases": True,
         "database_name": None, "db_role": None, "team_id": None, "valid_until": None,
         "expired": False, "not_started": False}]
    monkeypatch.setattr(access, "is_admin", lambda pid: False)
    monkeypatch.setattr(access.db, "fetch_all", lambda sql, params=None: rows)
    got = access.resolve_databases(UID, [(53, "ledger"), (53, "audit")])
    assert got[(53, "ledger")][0]["tier"] == "rw"    # its own row and the every-db row
    assert got[(53, "audit")][0]["tier"] == "ro"     # only the every-db row covers it
    assert got[(53, "ledger")][1] is rows[0]         # provenance: the row that won


def test_resolve_databases_answers_an_admin_without_reading(monkeypatch):
    monkeypatch.setattr(access, "is_admin", lambda pid: True)
    monkeypatch.setattr(access.db, "fetch_all", lambda *a, **k: pytest.fail("no read"))
    got = access.resolve_databases(UID, [(53, "ledger")])
    assert got[(53, "ledger")][0]["tier"] == "ddl"


# --- the team view -----------------------------------------------------------

@pytest.fixture
def team(monkeypatch):
    rows = [
        {"target_id": 53, "database_name": "ledger", "all_databases": False, "tier": "rw",
         "auto_approve": False, "valid_until": None},
        {"target_id": 53, "database_name": "audit", "all_databases": False, "tier": "ro",
         "auto_approve": False, "valid_until": None},
        {"target_id": 53, "database_name": "ledger", "all_databases": False, "tier": "ro",
         "auto_approve": True, "valid_until": None}]
    members = [{"slack_id": "U0EXAMPLE002", "name": "Example Member", "enabled": True}]
    approvers = [
        {"slack_id": "U0EXAMPLE003", "name": "Example Lead", "scope_target_id": 53,
         "all_targets": False, "max_tier": "ro", "any_tier": False},
        {"slack_id": "U0EXAMPLE003", "name": "Example Lead", "scope_target_id": 60,
         "all_targets": False, "max_tier": "ro", "any_tier": False}]
    audit_rows = []
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, a: UID)
    monkeypatch.setattr(ra, "_team_for_write", lambda tid: None if tid == 999 else
                        {"id": tid, "name": "team-alpha", "display_name": "Team Alpha", "source": None})
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: True)
    monkeypatch.setattr(ra, "_team_rows_v2", lambda tid: (rows, members, approvers))
    monkeypatch.setattr(ra.targets, "list_all", lambda: [T53, T60])

    class Txn:
        def __enter__(self): return None
        def __exit__(self, *e): return False
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn())
    monkeypatch.setattr(ra.audit, "log_in", lambda *a, **k: audit_rows.append(a[4]))
    return audit_rows


def test_the_team_view_splits_grants_from_waivers_and_names_the_leads(team):
    r = ra.admin_team_effective_access(7, claims={"sub": UID})
    [acc] = r["access"]
    assert acc["connectionId"] == "prod-ledger" and acc["mixedTiers"] is True
    assert {(d["database"], d["tier"]) for d in acc["perDatabase"]} == {("ledger", "RW"), ("audit", "RO")}
    assert r["autoApprove"] == [{"connectionId": "prod-ledger", "databaseId": "ledger",
                                 "allDatabases": False, "tier": "RO", "expiresAt": None}]
    [lead] = r["approvers"]
    assert lead["scopeTargets"] == ["prod-ledger", "prod-orders"] and lead["maxTier"] == "RO"
    assert r["team"]["name"] == "Team Alpha"
    assert team == ["team_effective_access_viewed"]


def test_an_unknown_team_is_a_404(team):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        ra.admin_team_effective_access(999, claims={"sub": UID})
    assert e.value.status_code == 404


# --- copy access -------------------------------------------------------------

def test_copying_team_access_reads_the_live_model_and_never_widens():
    src = inspect.getsource(ra.admin_copy_access)
    i = src.index("if teams_mod.use_v2():")
    v2 = src[i:src.index("else:", i)]
    assert "FROM access_grant g" in v2
    assert "ORDER BY tr.rank ASC" in v2             # the LOWEST tier on disagreement
    assert "NOT g.auto_approve" in v2
