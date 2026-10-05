"""Auto-approve only where the subject can already query.

A waiver waives the WAIT; it grants no access (access.py, rule 1). Written on
a server the subject cannot reach it decides nothing, sits in the table
reading as an exemption, and arrives pre-authorised the day they are granted
that server. The Auto-approve form now offers only what the subject reaches,
and both write routes refuse the rest, by name, from the resolvers the
effective-access screens ask.

Routes are called directly with fake claims -- no TestClient, no DB.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import auto_approve
from queryhub.web import routes_admin as ra

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Example Admin"}
TEAM = {"id": 7, "name": "Team Alpha"}


class Cur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return {"id": 1}


class Txn:
    def __init__(self, cur): self.cur = cur
    def __enter__(self): return self.cur
    def __exit__(self, *e): return False


@pytest.fixture
def env(monkeypatch):
    st = SimpleNamespace(cur=Cur(), reach={}, asked=[])

    def reach(subject, scopes):
        st.asked.append((subject, list(scopes)))
        return {s: st.reach.get(s) for s in scopes}

    monkeypatch.setattr(ra.admin, "require_admin", lambda c, a: CLAIMS["sub"])
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: True)
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn(st.cur))
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: [])
    monkeypatch.setattr(ra.audit, "log_in", lambda *a, **k: None)
    monkeypatch.setattr(ra, "_target_id_of", lambda c: {"prod-ledger": 53, "prod-orders": 60}.get(c))
    monkeypatch.setattr(ra, "_resolve_team", lambda n: TEAM if n == "Team Alpha" else None)
    monkeypatch.setattr(auto_approve, "validate_scope", lambda tid, dbn: None)
    monkeypatch.setattr(ra, "_person_reach", reach)
    monkeypatch.setattr(ra, "_team_reach", lambda team_id, scopes: reach(team_id, scopes))
    return st


def _bulk(**kw):
    kw.setdefault("subject", "U0EXAMPLE002")
    kw.setdefault("targets", [{"connectionId": "prod-ledger", "databaseId": "ledger"},
                              {"connectionId": "prod-orders"}])
    return ra.admin_bulk_create_auto_grants(ra.BulkAutoGrantIn(**kw), claims=CLAIMS)


# --- the bulk route ----------------------------------------------------------

def test_a_target_they_cannot_query_is_refused_by_name_and_nothing_is_written(env):
    env.reach = {(53, "ledger"): None, (60, None): "rw"}
    with pytest.raises(HTTPException) as e:
        _bulk()
    assert e.value.status_code == 409
    [r] = e.value.detail["refused"]
    assert r["target"] == "prod-ledger/ledger"
    assert "prod-ledger/ledger" in r["reason"] and "Grant access first" in r["reason"]
    # The form marks the row it came from with these two.
    assert (r["connectionId"], r["databaseId"]) == ("prod-ledger", "ledger")
    assert env.cur.sql == []


def test_a_tier_above_what_they_hold_there_is_refused(env):
    env.reach = {(53, "ledger"): "ro", (60, None): "rw"}
    with pytest.raises(HTTPException) as e:
        _bulk(tier="rw")
    [r] = e.value.detail["refused"]
    assert r["target"] == "prod-ledger/ledger"
    assert "run only RO on prod-ledger/ledger" in r["reason"]
    assert env.cur.sql == []


def test_what_they_can_query_at_the_tier_asked_is_written(env):
    env.reach = {(53, "ledger"): "rw", (60, None): "ro"}
    out = _bulk(tier="ro")
    assert out["applied"] == 2
    assert env.asked == [("U0EXAMPLE002", [(53, "ledger"), (60, None)])]


def test_a_team_is_asked_about_its_own_grants(env):
    env.reach = {(53, "ledger"): "ro"}
    with pytest.raises(HTTPException) as e:
        _bulk(subjectType="team", subject="Team Alpha")
    assert env.asked[0][0] == 7                          # the team's id, not a person
    [r] = e.value.detail["refused"]
    assert r["target"] == "prod-orders" and r["reason"].startswith("Team Team Alpha cannot query")


def test_an_unknown_connection_also_names_where_it_came_from(env):
    with pytest.raises(HTTPException) as e:
        _bulk(targets=[{"connectionId": "nope", "databaseId": "x"}])
    [r] = e.value.detail["refused"]
    assert (r["target"], r["connectionId"], r["databaseId"]) == ("nope/x", "nope", "x")


def test_a_target_refused_twice_is_named_once(env, monkeypatch):
    """Unreachable AND already held by the team is one problem with one row."""
    env.reach = {(53, "ledger"): None, (60, None): "ro"}
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: [
        {"target_id": 53, "database_name": "ledger"}])
    with pytest.raises(HTTPException) as e:
        _bulk(subjectType="team", subject="Team Alpha")
    assert [r["target"] for r in e.value.detail["refused"]] == ["prod-ledger/ledger"]


# --- the single route --------------------------------------------------------

def _single(**kw):
    body = {"user": "U0EXAMPLE002", "connectionId": "prod-ledger", "databaseId": "ledger",
            "tier": "ro"}
    body.update(kw)
    return ra.admin_create_auto_grant(ra.AutoGrantIn(**body), claims=CLAIMS)


def test_the_single_route_refuses_the_same_way(env):
    env.reach = {(53, "ledger"): None}
    with pytest.raises(HTTPException) as e:
        _single()
    assert e.value.status_code == 409
    assert "prod-ledger/ledger" in e.value.detail["message"]
    assert e.value.detail["refused"][0]["target"] == "prod-ledger/ledger"
    assert env.cur.sql == []


def test_the_single_route_writes_where_they_can_query(env):
    env.reach = {(53, "ledger"): "ro"}
    assert _single()["id"] == "1"
    assert any("INSERT INTO auto_approve_grants" in q for q, _ in env.cur.sql)


# --- the resolvers -----------------------------------------------------------

def test_a_fleet_wide_waiver_is_not_checked_against_any_server(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a waiver with no target names no server to check")
    monkeypatch.setattr(ra, "_person_reach", boom)
    assert ra._reach_refusals("U0EXAMPLE002", None, "ro", [
        {"tid": None, "db": None, "label": "every server", "connectionId": None,
         "databaseId": None}]) == []


def test_a_person_reach_asks_the_effective_access_resolvers(monkeypatch):
    """New model: the server-level answer from `effective_grants_for_user`, the
    per-database tier from `resolve_databases` -- the two the effective-access
    screen reads -- and only for servers they reach at all."""
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: True)
    monkeypatch.setattr(ra.teams, "effective_grants_for_user", lambda pid, ids: {
        53: {"mode": "rw", "allowed_databases": None, "source": "user"}, 60: None})
    asked = []

    def resolve_databases(pid, pairs):
        asked.append(list(pairs))
        return {p: ({"tier": "ro"} if p == (53, "ledger") else None, None) for p in pairs}
    monkeypatch.setattr(ra.access_model, "resolve_databases", resolve_databases)

    got = ra._person_reach("U0EXAMPLE002", [(53, "ledger"), (53, "audit"), (53, None),
                                            (60, None), (60, "orders")])
    assert got == {(53, "ledger"): "ro", (53, "audit"): None, (53, None): "rw",
                   (60, None): None, (60, "orders"): None}
    assert asked == [[(53, "ledger"), (53, "audit")]]


def test_a_person_reach_under_the_old_model_uses_its_tier_authority(monkeypatch):
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: False)
    monkeypatch.setattr(ra.teams, "effective_grants_for_user", lambda pid, ids: {
        53: {"mode": "rw", "allowed_databases": {"ledger"}, "source": "user"}})
    monkeypatch.setattr(ra.teams, "effective_mode_for_database",
                        lambda pid, tid, dbn: "rw" if dbn == "ledger" else None)
    assert ra._person_reach("U0EXAMPLE002", [(53, "ledger"), (53, "audit")]) == {
        (53, "ledger"): "rw", (53, "audit"): None}


def test_a_team_reach_reads_its_grants_and_never_its_waivers(monkeypatch):
    rows = [
        {"target_id": 53, "database_name": "ledger", "all_databases": False, "tier": "rw",
         "auto_approve": False},
        {"target_id": 53, "database_name": None, "all_databases": True, "tier": "ro",
         "auto_approve": False},
        # A waiver is not access: a team holding only this reaches nothing on 60.
        {"target_id": 60, "database_name": None, "all_databases": True, "tier": "rw",
         "auto_approve": True},
    ]
    monkeypatch.setattr(ra, "_team_rows_v2", lambda team_id: (rows, [], []))
    got = ra._team_reach(7, [(53, "ledger"), (53, "audit"), (53, None), (60, None)])
    assert got == {(53, "ledger"): "rw", (53, "audit"): "ro", (53, None): "rw",
                   (60, None): None}
