"""The connection tree shows the tier of each database, not of the target.

Submit authorizes per database (`teams.effective_mode_for_database`). The tree
used to put the target's top tier on every database, so RW on one database and
RO on another of the same target showed RW on both. The tier now comes from
the grant rows the payload already reads, so it costs no query per database.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from queryhub import access, targets, teams
from queryhub.web import app as web_app
from queryhub.web import deps, routes_data

UID = "U0EXAMPLE003"


def _boom(*a, **k):
    raise AssertionError("the tier must come from the rows already read")


# ---------------------------------------------------------------------------
# the helper
# ---------------------------------------------------------------------------

def _row(mode, dbs=None, ends_at=None):
    return {"mode": mode, "allowed_databases": None if dbs is None else set(dbs),
            "ends_at": ends_at}


def test_rw_on_one_database_and_ro_on_another_are_two_tiers(monkeypatch):
    monkeypatch.setattr(teams.db, "fetch_all", _boom)
    monkeypatch.setattr(teams.db, "fetch_one", _boom)
    grant = {"mode": "rw", "allowed_databases": {"a", "b"}, "source": "team",
             "grant_rows": [_row("rw", ["a"]), _row("ro", ["b"])]}
    assert teams.database_modes(UID, 1, grant, ["a", "b"]) == {"a": "rw", "b": "ro"}


def test_an_every_database_row_lifts_only_up_to_its_own_tier(monkeypatch):
    monkeypatch.setattr(teams.db, "fetch_all", _boom)
    grant = {"mode": "rw", "allowed_databases": None, "source": "team",
             "grant_rows": [_row("ro"), _row("rw", ["a"])]}
    assert teams.database_modes(UID, 1, grant, ["a", "b"]) == {"a": "rw", "b": "ro"}


def test_an_ended_row_does_not_count(monkeypatch):
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    grant = {"mode": "rw", "allowed_databases": {"a"}, "source": "team",
             "grant_rows": [_row("rw", ["a"], ends_at=past), _row("ro", ["a"])]}
    assert teams.database_modes(UID, 1, grant, ["a"]) == {"a": "ro"}


def test_an_admin_without_rows_keeps_the_target_tier(monkeypatch):
    monkeypatch.setattr(teams.db, "fetch_all", _boom)
    monkeypatch.setattr(teams.db, "fetch_one", _boom)
    grant = {"mode": "ddl", "allowed_databases": None, "source": "admin_or_bypass"}
    assert teams.database_modes(UID, 1, grant, ["a", "b"]) == {"a": "ddl", "b": "ddl"}


def test_a_grant_resolved_one_by_one_asks_per_database(monkeypatch):
    """A super-admin-only target comes back from the single resolver, with no
    rows. Its databases go through the tier authority itself."""
    asked = []

    def per_db(pid, tid, d):
        asked.append((tid, d))
        return {"a": "rw", "b": "ro"}[d]
    monkeypatch.setattr(teams, "effective_mode_for_database", per_db)
    grant = {"mode": "rw", "allowed_databases": {"a", "b"}, "source": "team"}
    assert teams.database_modes(UID, 4, grant, ["a", "b"]) == {"a": "rw", "b": "ro"}
    assert asked == [(4, "a"), (4, "b")]


def test_no_grant_is_no_tier():
    assert teams.database_modes(UID, 1, None, ["a"]) == {"a": None}


# ---------------------------------------------------------------------------
# legacy model: same answer as the tier authority, two queries in total
# ---------------------------------------------------------------------------

TEAM_ROWS = [
    {"target_server_id": 1, "mode": "rw", "allowed_databases": ["a"], "expires_at": None},
    {"target_server_id": 1, "mode": "ro", "allowed_databases": ["b"], "expires_at": None},
    {"target_server_id": 2, "mode": "ro", "allowed_databases": None, "expires_at": None},
    {"target_server_id": 2, "mode": "ddl", "allowed_databases": ["c"], "expires_at": None},
]


def test_legacy_tree_agrees_with_the_tier_authority(monkeypatch):
    monkeypatch.setattr(teams, "use_v2", lambda: False)
    monkeypatch.setattr(teams, "_is_unrestricted", lambda pid: False)
    monkeypatch.setattr(teams, "_unrestricted_on", lambda pid, tid: False)
    seen = []

    def fetch_all(sql, params=None):
        seen.append(sql)
        if "user_target_grants" in sql:
            return []
        rows = TEAM_ROWS
        if params and len(params) == 2 and isinstance(params[1], int):
            rows = [r for r in rows if r["target_server_id"] == params[1]]
        return [dict(r) for r in rows]
    monkeypatch.setattr(teams.db, "fetch_all", fetch_all)
    monkeypatch.setattr(teams.db, "fetch_one", lambda sql, params=None: None)

    grants = teams.effective_grants_for_user(UID, [1, 2])
    tree = {1: teams.database_modes(UID, 1, grants[1], ["a", "b"]),
            2: teams.database_modes(UID, 2, grants[2], ["c", "d", "e"])}
    assert len(seen) == 2, "the tree must not add a query per database"
    assert tree == {1: {"a": "rw", "b": "ro"},
                    2: {"c": "ddl", "d": "ro", "e": "ro"}}
    for tid, modes in tree.items():
        for d, mode in modes.items():
            assert teams.effective_mode_for_database(UID, tid, d) == mode, (tid, d)


# ---------------------------------------------------------------------------
# v2 model: same answer as access.resolve, one query in total
# ---------------------------------------------------------------------------

def _g(tid, tier, db=None, mine=False, merge=False, auto=False):
    rank = {"ro": 10, "rw": 20, "ddl": 30}[tier]
    return {"target_id": tid, "mine": mine, "tier": tier, "rank": rank,
            "auto_approve": auto, "merge_with_team": merge,
            "all_targets": False, "all_databases": db is None,
            "database_name": db, "db_role": None, "valid_until": None,
            "expired": False, "not_started": False}


V2_ROWS = [
    _g(1, "rw", "a"), _g(1, "ro", "b"),                    # team rows
    _g(2, "ddl", "c"), _g(2, "ro", "c", mine=True),        # own row displaces team
    _g(3, "ro"), _g(3, "rw", "a", mine=True, merge=True),  # own row merges
    _g(3, "ddl", "b", auto=True),                          # a waiver is no grant
]


def test_v2_tree_agrees_with_access_resolve(monkeypatch):
    monkeypatch.setattr(teams, "use_v2", lambda: True)
    monkeypatch.setattr(access, "is_admin", lambda pid: False)
    monkeypatch.setattr(access, "explicit_only", lambda pid, tid: False)
    seen = []
    monkeypatch.setattr(access.db, "fetch_all",
                        lambda sql, params=None: seen.append(sql) or
                        [dict(r) for r in V2_ROWS])

    grants = teams.effective_grants_for_user(UID, [1, 2, 3])
    tree = {tid: teams.database_modes(UID, tid, grants[tid], ["a", "b", "c"])
            for tid in (1, 2, 3)}
    assert len(seen) == 1, "the tree must not add a query per database"
    assert tree[1] == {"a": "rw", "b": "ro", "c": None}
    assert tree[2] == {"a": None, "b": None, "c": "ro"}
    assert tree[3] == {"a": "rw", "b": "ro", "c": "ro"}

    def covering(pid, tid, database_name, explicit=False):
        return [dict(r) for r in V2_ROWS if r["target_id"] == tid]
    monkeypatch.setattr(access, "_covering", covering)
    for tid, modes in tree.items():
        for d, mode in modes.items():
            got = access.resolve(UID, tid, d)
            assert (got["tier"] if got else None) == mode, (tid, d)


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    logging.disable(logging.CRITICAL)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": UID, "provider": "slack", "sid": "s1", "name": "Ex"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(routes_data.admins, "is_super_admin", lambda uid: False)
    yield TestClient(app)
    logging.disable(logging.NOTSET)


def test_the_payload_carries_each_database_tier(web, monkeypatch):
    fleet = [targets.TargetServer(id=7, alias="prod-ledger", host="h.example.test",
                                  port=5432, default_database="a", username="u",
                                  enabled=True, notes=None, engine="postgres",
                                  replica_of=None)]
    monkeypatch.setattr(routes_data.targets, "list_all", lambda: fleet)
    monkeypatch.setattr(routes_data.targets, "list_enabled", lambda: fleet)
    monkeypatch.setattr(routes_data.db, "fetch_all", lambda sql, p=None: [])
    monkeypatch.setattr(
        routes_data.teams, "effective_grants_for_user",
        lambda uid, ids: {7: {"mode": "rw", "allowed_databases": {"a", "b"},
                              "source": "team",
                              "grant_rows": [_row("rw", ["a"]), _row("ro", ["b"])]}})
    monkeypatch.setattr(routes_data, "_catalog_databases_map",
                        lambda ids: {7: ["a", "b"]})
    monkeypatch.setattr(routes_data.auto_approve, "active_grants", lambda uid: [])
    monkeypatch.setattr(routes_data, "_catalog_table_refs_map", lambda pairs: {})
    monkeypatch.setattr(routes_data, "_catalog_functions_map", lambda pairs: {})
    monkeypatch.setattr(routes_data.teams, "effective_mode_for_database", _boom)

    r = web.get("/api/connections")
    assert r.status_code == 200, r.text
    conn = r.json()["connections"][0]
    assert {d["id"]: d["tier"] for d in conn["databases"]} == {"a": "RW", "b": "RO"}
