"""Connection owners from the admin screen, run against a real database.

Adding an owner by hand must make the team's lead an approver for that one
target at once, and removing it must take back only what it gave. The rules
live in SQL (the unique index, the source column, the enabled filter), so they
run here and not against a copy.

Every row is keyed to names and ids that no real target, team or person has,
and only those rows are removed. The audit log is append-only, so its rows
stay behind.
"""
import os

import pytest
from fastapi import HTTPException

from queryhub import db, owner_approvers
from queryhub.web import routes_admin as ra

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

ALIASES = ("it-owner-one", "it-owner-two", "it-owner-off")
TEAMS = ("it-owner-alpha", "it-owner-beta", "it-owner-gamma")
PEOPLE = (("IT Owner Lead A", "U0OWNERA"), ("IT Owner Lead B", "U0OWNERB"),
          ("IT Owner Admin", "U0OWNERZ"))
CLAIMS = {"sub": "U0OWNERZ", "name": "IT Owner Admin"}


def _wipe():
    t = [r["id"] for r in db.fetch_all(
        "SELECT id FROM target_servers WHERE alias = ANY(%s)", (list(ALIASES),))]
    tm = [r["id"] for r in db.fetch_all(
        "SELECT id FROM team WHERE name = ANY(%s)", (list(TEAMS),))]
    p = [r["id"] for r in db.fetch_all(
        "SELECT id FROM principal WHERE display_name = ANY(%s)",
        ([n for n, _ in PEOPLE],))]
    db.execute("DELETE FROM role_assignment WHERE principal_id = ANY(%s) "
               "   OR scope_target_id = ANY(%s)", (p, t))
    db.execute("DELETE FROM target_team WHERE target_id = ANY(%s) "
               "   OR team_id = ANY(%s)", (t, tm))
    db.execute("DELETE FROM team_member WHERE team_id = ANY(%s)", (tm,))
    db.execute("DELETE FROM principal_identity WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal WHERE id = ANY(%s)", (p,))
    db.execute("DELETE FROM team WHERE id = ANY(%s)", (tm,))
    db.execute("DELETE FROM target_servers WHERE id = ANY(%s)", (t,))


@pytest.fixture
def world(monkeypatch):
    """Three targets (one disabled), three teams (gamma has no lead), two
    leads and the admin who makes the changes."""
    _wipe()
    monkeypatch.setattr(ra.admin, "require_admin",
                        lambda claims, scope, **kw: claims["sub"])
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO target_servers (alias, host, default_database, engine, enabled) "
            "VALUES (%s,'h1.example.internal','d','athena',true),"
            "       (%s,'h2.example.internal','d','athena',true),"
            "       (%s,'h3.example.internal','d','athena',false) RETURNING id, alias",
            ALIASES)
        t = {r["alias"]: r["id"] for r in cur.fetchall()}
        cur.execute("INSERT INTO team (name, display_name) VALUES "
                    "(%s,'IT Alpha'),(%s,'IT Beta'),(%s,'IT Gamma') RETURNING id, name",
                    TEAMS)
        tm = {r["name"]: r["id"] for r in cur.fetchall()}
        p = {}
        for name, sid in PEOPLE:
            cur.execute("INSERT INTO principal (kind, display_name, enabled) "
                        "VALUES ('person', %s, true) RETURNING id", (name,))
            p[name] = cur.fetchone()["id"]
            cur.execute("INSERT INTO principal_identity (principal_id, provider, external_id) "
                        "VALUES (%s, 'slack', %s)", (p[name], sid))
        cur.execute("INSERT INTO team_member (team_id, principal_id, is_lead) "
                    "VALUES (%s,%s,true),(%s,%s,true)",
                    (tm["it-owner-alpha"], p["IT Owner Lead A"],
                     tm["it-owner-beta"], p["IT Owner Lead B"]))
    yield {"t": t, "tm": tm, "p": p}
    _wipe()


def _live(principal_id, target_id):
    return db.fetch_all(
        "SELECT id, source, max_tier, all_teams, created_by FROM role_assignment "
        " WHERE role = 'approver' AND principal_id = %s AND scope_target_id = %s "
        "   AND revoked_at IS NULL AND NOT is_deleted", (principal_id, target_id))


def _status(fn, *args):
    with pytest.raises(HTTPException) as e:
        fn(*args)
    return e.value.status_code, str(e.value.detail)


def test_adding_an_owner_makes_its_lead_an_approver_at_once(world):
    t, tm, p = world["t"], world["tm"], world["p"]
    before = db.fetch_one("SELECT count(*) AS n FROM auth_event_outbox")["n"]
    r = ra.admin_connection_owner_add("it-owner-one", ra.OwnerIn(teamId=tm["it-owner-alpha"]), CLAIMS)
    assert r["changed"]["approversAdded"] == ["IT Owner Lead A"]
    assert r["approvers"] == [{"name": "IT Owner Lead A", "maxTier": "RO"}]
    assert r["owners"][0]["syncedFrom"] is None                   # hand-made
    rows = _live(p["IT Owner Lead A"], t["it-owner-one"])
    assert len(rows) == 1 and rows[0]["source"] == owner_approvers.SOURCE
    assert rows[0]["all_teams"] and rows[0]["created_by"] == p["IT Owner Admin"]
    audit = db.fetch_one("SELECT details FROM audit_log WHERE action = 'target_owner_added' "
                         " AND details->>'target' = 'it-owner-one' ORDER BY id DESC LIMIT 1")
    assert audit["details"]["approversAdded"] == ["IT Owner Lead A"]
    # The lead hears about it: the DM path is not suppressed.
    assert db.fetch_one("SELECT count(*) AS n FROM auth_event_outbox")["n"] > before


def test_a_second_add_and_unknown_names_are_refused(world):
    tm = world["tm"]
    ra.admin_connection_owner_add("it-owner-one", ra.OwnerIn(teamId=tm["it-owner-alpha"]), CLAIMS)
    assert _status(ra.admin_connection_owner_add, "it-owner-one",
                   ra.OwnerIn(teamId=tm["it-owner-alpha"]), CLAIMS)[0] == 409
    assert _status(ra.admin_connection_owner_add, "it-owner-one",
                   ra.OwnerIn(teamId=-1), CLAIMS)[0] == 404
    assert _status(ra.admin_connection_owners, "it-owner-nope", CLAIMS)[0] == 404


def test_a_team_without_a_lead_is_named_not_invented(world):
    r = ra.admin_connection_owner_add("it-owner-one", ra.OwnerIn(teamId=world["tm"]["it-owner-gamma"]), CLAIMS)
    assert r["changed"]["approversAdded"] == []
    assert any("no lead" in n for n in r["changed"]["notes"])


def test_a_row_from_another_source_is_left_alone(world):
    t, tm, p = world["t"], world["tm"], world["p"]
    db.execute("INSERT INTO role_assignment (principal_id, role, scope_team_id, all_teams, "
               " scope_target_id, all_targets, max_tier, any_tier, reason, source) "
               "VALUES (%s,'approver',NULL,TRUE,%s,FALSE,'ro',FALSE,'by hand',NULL)",
               (p["IT Owner Lead B"], t["it-owner-two"]))
    r = ra.admin_connection_owner_add("it-owner-two", ra.OwnerIn(teamId=tm["it-owner-beta"]), CLAIMS)
    assert r["changed"]["approversAdded"] == []
    assert any("another source" in n for n in r["changed"]["notes"])
    assert len(_live(p["IT Owner Lead B"], t["it-owner-two"])) == 1
    r = ra.admin_connection_owner_remove("it-owner-two", tm["it-owner-beta"], CLAIMS)
    assert r["changed"]["approversRevoked"] == []                 # not ours to take
    assert len(_live(p["IT Owner Lead B"], t["it-owner-two"])) == 1


def test_a_synced_owner_is_refused_with_the_sync_named(world):
    t, tm = world["t"], world["tm"]
    db.execute("INSERT INTO target_team (target_id, team_id, source) VALUES (%s,%s,'pod-sync')",
               (t["it-owner-two"], tm["it-owner-alpha"]))
    code, msg = _status(ra.admin_connection_owner_remove, "it-owner-two", tm["it-owner-alpha"], CLAIMS)
    assert code == 409 and "pod-sync" in msg


def test_removing_an_owner_revokes_what_it_gave(world):
    t, tm, p = world["t"], world["tm"], world["p"]
    ra.admin_connection_owner_add("it-owner-one", ra.OwnerIn(teamId=tm["it-owner-alpha"]), CLAIMS)
    r = ra.admin_connection_owner_remove("it-owner-one", tm["it-owner-alpha"], CLAIMS)
    assert r["changed"]["approversRevoked"] == ["IT Owner Lead A"]
    assert r["owners"] == [] and r["approvers"] == []
    gone = db.fetch_one("SELECT revoked_by FROM role_assignment WHERE principal_id = %s "
                        "   AND scope_target_id = %s AND revoked_at IS NOT NULL",
                        (p["IT Owner Lead A"], t["it-owner-one"]))
    assert gone["revoked_by"] == p["IT Owner Admin"]


def test_a_change_reconciles_its_own_target_and_no_other(world):
    t, tm, p = world["t"], world["tm"], world["p"]
    drift = db.insert_returning(
        "INSERT INTO role_assignment (principal_id, role, scope_team_id, all_teams, "
        " scope_target_id, all_targets, max_tier, any_tier, reason, source) "
        "VALUES (%s,'approver',NULL,TRUE,%s,FALSE,'ro',FALSE,'drift',%s) RETURNING id",
        (p["IT Owner Lead B"], t["it-owner-off"], owner_approvers.SOURCE))["id"]
    ra.admin_connection_owner_add("it-owner-one", ra.OwnerIn(teamId=tm["it-owner-alpha"]), CLAIMS)
    assert db.fetch_one("SELECT revoked_at FROM role_assignment WHERE id = %s",
                        (drift,))["revoked_at"] is None
    # A disabled target gets no approver, and its own stale row goes.
    r = ra.admin_connection_owner_add("it-owner-off", ra.OwnerIn(teamId=tm["it-owner-alpha"]), CLAIMS)
    assert r["changed"]["approversAdded"] == []
    assert r["changed"]["approversRevoked"] == ["IT Owner Lead B"]


def test_the_screen_and_the_fleet_run_agree(world):
    t, tm = world["t"], world["tm"]
    ra.admin_connection_owner_add("it-owner-one", ra.OwnerIn(teamId=tm["it-owner-alpha"]), CLAIMS)
    with db.transaction() as cur:
        tier = owner_approvers.ceiling(cur)
        p = owner_approvers.plan(cur, owner_approvers.SOURCE, tier)
        mine = set(t.values())
        assert not [k for k in p["add"] + p["drop"] + p["retier"] if k[2] in mine]
        cur.connection.rollback()
