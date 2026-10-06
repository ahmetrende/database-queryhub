"""A super-admin-only target (migration 141), run against a real database.

On a flagged target, fleet-wide grants, fleet-wide waivers and the role of an
admin who is not a super-admin do not count. A grant that names the target
does: that is the deliberate exception. Every resolver that answers "can this
person use this target" has to agree, so each one is asked here.

Every row is keyed to names no real target, team or person has, and only those
rows are removed.
"""
import os

import pytest
from fastapi import HTTPException

from queryhub import access, admins, auto_approve, db, targets
from queryhub.web import routes_admin as ra

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

OPEN, FLAG = "it-sao-open", "it-sao-flag"
TEAM = "it-sao-team"
PEOPLE = {  # name -> slack id
    "IT SAO Super": "U0SAOSUP", "IT SAO Admin": "U0SAOADM", "IT SAO Fleet": "U0SAOFLT",
    "IT SAO Named": "U0SAONAM", "IT SAO Member": "U0SAOMEM",
}


def _wipe():
    t = [r["id"] for r in db.fetch_all("SELECT id FROM target_servers WHERE alias = ANY(%s)",
                                       ([OPEN, FLAG],))]
    tm = [r["id"] for r in db.fetch_all("SELECT id FROM team WHERE name = %s", (TEAM,))]
    p = [r["id"] for r in db.fetch_all("SELECT id FROM principal WHERE display_name = ANY(%s)",
                                       (list(PEOPLE),))]
    db.execute("DELETE FROM access_grant WHERE principal_id = ANY(%s) OR team_id = ANY(%s) "
               "   OR target_id = ANY(%s)", (p, tm, t))
    db.execute("DELETE FROM role_assignment WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM team_member WHERE team_id = ANY(%s)", (tm,))
    db.execute("DELETE FROM principal_identity WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal WHERE id = ANY(%s)", (p,))
    db.execute("DELETE FROM team WHERE id = ANY(%s)", (tm,))
    db.execute("DELETE FROM target_servers WHERE id = ANY(%s)", (t,))


@pytest.fixture
def world():
    _wipe()
    with db.transaction() as cur:
        cur.execute("INSERT INTO target_servers (alias, host, default_database, engine, enabled, "
                    " super_admin_only) VALUES (%s,'o.example.internal','d','athena',true,false), "
                    " (%s,'f.example.internal','d','athena',true,true) RETURNING id, alias",
                    (OPEN, FLAG))
        t = {r["alias"]: r["id"] for r in cur.fetchall()}
        cur.execute("INSERT INTO team (name, display_name) VALUES (%s,'IT SAO Team') RETURNING id",
                    (TEAM,))
        team = cur.fetchone()["id"]
        p = {}
        for name, sid in PEOPLE.items():
            cur.execute("INSERT INTO principal (kind, display_name, enabled) "
                        "VALUES ('person', %s, true) RETURNING id", (name,))
            p[name] = cur.fetchone()["id"]
            cur.execute("INSERT INTO principal_identity (principal_id, provider, external_id) "
                        "VALUES (%s, 'slack', %s)", (p[name], sid))
        role = ("INSERT INTO role_assignment (principal_id, role, all_teams, all_targets, "
                " max_tier, any_tier) VALUES (%s, 'admin', TRUE, TRUE, %s, %s)")
        cur.execute(role, (p["IT SAO Super"], None, True))       # unscoped: a super-admin
        cur.execute(role, (p["IT SAO Admin"], "ro", False))      # capped: not a super-admin
        grant = ("INSERT INTO access_grant (principal_id, team_id, target_id, all_targets, "
                 " database_name, all_databases, tier, auto_approve) "
                 "VALUES (%s, %s, %s, %s, NULL, TRUE, %s, %s)")
        cur.execute(grant, (p["IT SAO Fleet"], None, None, True, "ddl", False))
        cur.execute(grant, (p["IT SAO Fleet"], None, None, True, "ro", True))
        cur.execute(grant, (p["IT SAO Named"], None, t[FLAG], False, "ro", False))
        cur.execute(grant, (p["IT SAO Named"], None, t[FLAG], False, "ro", True))
        cur.execute(grant, (None, team, None, True, "ddl", False))
        cur.execute("INSERT INTO team_member (team_id, principal_id) VALUES (%s, %s)",
                    (team, p["IT SAO Member"]))
    yield t
    _wipe()


def _sid(name):
    return PEOPLE[name]


def test_a_super_admin_keeps_the_flagged_target(world):
    got = access.resolve_target(_sid("IT SAO Super"), world[FLAG])
    assert got and got["tier"] == "ddl"
    assert FLAG in {x.alias for x in access.visible_targets(_sid("IT SAO Super"))}


def test_a_capped_admin_loses_it_and_keeps_the_rest(world):
    sid = _sid("IT SAO Admin")
    assert access.resolve_target(sid, world[FLAG]) is None
    assert access.resolve_target(sid, world[OPEN])["tier"] == "ddl"
    assert FLAG not in {x.alias for x in access.visible_targets(sid)}
    many = access.resolve_many(sid, [world[OPEN], world[FLAG]])
    assert many[world[FLAG]] is None and many[world[OPEN]]["tier"] == "ddl"
    pairs = access.resolve_databases(sid, [(world[FLAG], "d"), (world[OPEN], "d")])
    assert pairs[(world[FLAG], "d")][0] is None
    assert pairs[(world[OPEN], "d")][0]["tier"] == "ddl"


def test_a_fleet_wide_grant_stops_there(world):
    sid = _sid("IT SAO Fleet")
    assert access.resolve_target(sid, world[FLAG]) is None
    assert access.resolve(sid, world[FLAG], "d") is None
    assert access.resolve_target(sid, world[OPEN])["tier"] == "ddl"
    assert FLAG not in {x.alias for x in access.visible_targets(sid)}
    assert FLAG not in {x.alias for x in access.search_visible_targets(sid, "it-sao")}


def test_a_team_fleet_wide_grant_stops_there_too(world):
    sid = _sid("IT SAO Member")
    assert access.resolve_target(sid, world[FLAG]) is None
    assert access.resolve_target(sid, world[OPEN])["tier"] == "ddl"


def test_a_grant_that_names_the_target_is_the_exception(world):
    sid = _sid("IT SAO Named")
    got = access.resolve_target(sid, world[FLAG])
    assert got and got["tier"] == "ro" and got["auto_tier"] == "ro"
    assert FLAG in {x.alias for x in access.visible_targets(sid)}


def test_a_fleet_wide_waiver_does_not_decide_there(world):
    flag, open_ = targets.by_alias(FLAG), targets.by_alias(OPEN)
    fleet = {"target_server_id": None, "max_tier": "ro"}
    named = {"target_server_id": world[FLAG], "max_tier": "ro"}
    assert auto_approve.waiver_applies(flag, fleet) is False
    assert auto_approve.waiver_applies(flag, named) is True
    assert auto_approve.waiver_applies(open_, fleet) is True


def test_only_a_super_admin_can_grant_on_it(world, monkeypatch):
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: uid == _sid("IT SAO Super"))
    with pytest.raises(HTTPException) as e:
        ra._refuse_flagged_unless_super(_sid("IT SAO Admin"), world[FLAG])
    assert e.value.status_code == 403
    ra._refuse_flagged_unless_super(_sid("IT SAO Super"), world[FLAG])     # no raise
    ra._refuse_flagged_unless_super(_sid("IT SAO Admin"), world[OPEN])     # no raise
