"""Grants on every server, and the super-admin-only switch, against a real DB.

`POST /admin/grants/fleet` writes one `all_targets` row for a person or a team,
read-only, with an optional fleet-wide waiver beside it. A super-admin-only
target (migration 141) stays outside it. Only a super-admin writes either.

Every row is keyed to names no real target, team or person has, and only those
rows are removed. The audit log is append-only, so its rows stay behind.
"""
import os

import pytest
from fastapi import HTTPException

from queryhub import access, admins, db, targets
from queryhub import teams as teams_mod
from queryhub.web import routes_admin as ra

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

OPEN, FLAG, TEAM = "it-flt-open", "it-flt-flag", "it-flt-team"
PEOPLE = {"IT FLT Person": "U0FLTPER", "IT FLT Member": "U0FLTMEM",
          "IT FLT Super": "U0FLTSUP", "IT FLT Admin": "U0FLTADM"}
SUPER = {"sub": "U0FLTSUP", "name": "IT FLT Super"}
ADMIN = {"sub": "U0FLTADM", "name": "IT FLT Admin"}


def _wipe():
    t = [r["id"] for r in db.fetch_all("SELECT id FROM target_servers WHERE alias = ANY(%s)",
                                       ([OPEN, FLAG],))]
    tm = [r["id"] for r in db.fetch_all("SELECT id FROM team WHERE name = %s", (TEAM,))]
    p = [r["id"] for r in db.fetch_all("SELECT id FROM principal WHERE display_name = ANY(%s)",
                                       (list(PEOPLE),))]
    db.execute("DELETE FROM access_grant WHERE principal_id = ANY(%s) OR team_id = ANY(%s)",
               (p, tm))
    db.execute("DELETE FROM team_member WHERE team_id = ANY(%s)", (tm,))
    db.execute("DELETE FROM principal_identity WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal WHERE id = ANY(%s)", (p,))
    db.execute("DELETE FROM team WHERE id = ANY(%s)", (tm,))
    db.execute("DELETE FROM target_servers WHERE id = ANY(%s)", (t,))


@pytest.fixture
def world(monkeypatch):
    _wipe()
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, scope, **kw: claims["sub"])
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: uid == SUPER["sub"])
    monkeypatch.setattr(teams_mod, "use_v2", lambda: True)
    with db.transaction() as cur:
        cur.execute("INSERT INTO target_servers (alias, host, default_database, engine, enabled, "
                    " super_admin_only) VALUES (%s,'o.example.internal','d','athena',true,false), "
                    " (%s,'f.example.internal','d','athena',true,true) RETURNING id, alias",
                    (OPEN, FLAG))
        t = {r["alias"]: r["id"] for r in cur.fetchall()}
        cur.execute("INSERT INTO team (name, display_name) VALUES (%s, 'IT FLT Team') RETURNING id",
                    (TEAM,))
        team = cur.fetchone()["id"]
        p = {}
        for name, sid in PEOPLE.items():
            cur.execute("INSERT INTO principal (kind, display_name, enabled) "
                        "VALUES ('person', %s, true) RETURNING id", (name,))
            p[name] = cur.fetchone()["id"]
            cur.execute("INSERT INTO principal_identity (principal_id, provider, external_id) "
                        "VALUES (%s, 'slack', %s)", (p[name], sid))
        cur.execute("INSERT INTO team_member (team_id, principal_id) VALUES (%s, %s)",
                    (team, p["IT FLT Member"]))
    yield {"t": t, "p": p, "team": team}
    _wipe()


def _status(fn, *args):
    with pytest.raises(HTTPException) as e:
        fn(*args)
    return e.value.status_code


def _mine(subject_name):
    return [g for g in ra.admin_fleet_grants(SUPER)["grants"] if g["subjectName"] == subject_name]


def test_a_person_gets_ro_everywhere_but_the_flagged_server(world):
    r = ra.admin_add_fleet_grant(ra.FleetGrantIn(subject="U0FLTPER", autoApprove=True), SUPER)
    assert r["id"] and r["waiverId"] and r["tier"] == "RO"
    open_ = access.resolve_target("U0FLTPER", world["t"][OPEN])
    assert open_["tier"] == "ro" and open_["auto_tier"] == "ro"
    assert access.resolve_target("U0FLTPER", world["t"][FLAG]) is None
    rows = _mine("IT FLT Person")
    assert sorted(g["autoApprove"] for g in rows) == [False, True]
    assert all(g["syncedFrom"] is None and g["subjectType"] == "user" for g in rows)


def test_refusals_write_nothing(world):
    ra.admin_add_fleet_grant(ra.FleetGrantIn(subject="U0FLTPER"), SUPER)
    assert _status(ra.admin_add_fleet_grant, ra.FleetGrantIn(subject="U0FLTPER"), SUPER) == 409
    assert _status(ra.admin_add_fleet_grant, ra.FleetGrantIn(subject="U0FLTPER", tier="rw"), SUPER) == 400
    assert _status(ra.admin_add_fleet_grant, ra.FleetGrantIn(subject="U0FLTPER"), ADMIN) == 403
    assert _status(ra.admin_add_fleet_grant, ra.FleetGrantIn(subject="U0NOBODY"), SUPER) == 404
    assert len(_mine("IT FLT Person")) == 1


def test_revoking_the_grant_takes_its_waiver_with_it(world):
    r = ra.admin_add_fleet_grant(ra.FleetGrantIn(subject="U0FLTPER", autoApprove=True), SUPER)
    assert _status(ra.admin_revoke_fleet_grant, r["id"], ADMIN) == 403
    ra.admin_revoke_fleet_grant(r["id"], SUPER)
    assert _mine("IT FLT Person") == []
    assert access.resolve_target("U0FLTPER", world["t"][OPEN]) is None


def test_a_mirrored_row_is_refused_with_its_source(world):
    gid = db.insert_returning(
        "INSERT INTO access_grant (principal_id, target_id, all_targets, database_name, "
        " all_databases, tier, auto_approve, mirrored_from) "
        "VALUES (%s, NULL, TRUE, NULL, TRUE, 'ddl', FALSE, 'requesters') RETURNING id",
        (world["p"]["IT FLT Person"],))["id"]
    assert _status(ra.admin_revoke_fleet_grant, gid, SUPER) == 409


def test_a_team_grant_reaches_its_members(world):
    ra.admin_add_fleet_grant(ra.FleetGrantIn(subjectType="team", subject=TEAM), SUPER)
    assert access.resolve_target("U0FLTMEM", world["t"][OPEN])["tier"] == "ro"
    assert access.resolve_target("U0FLTMEM", world["t"][FLAG]) is None
    assert [g["subjectType"] for g in _mine("IT FLT Team")] == ["team"]


def test_the_super_admin_only_switch(world):
    assert _status(ra.admin_set_super_admin_only, OPEN, ra.SuperAdminOnlyIn(on=True), ADMIN) == 403
    r = ra.admin_set_super_admin_only(OPEN, ra.SuperAdminOnlyIn(on=True), SUPER)
    assert r["superAdminOnly"] is True and r["changed"] is True
    assert ra.admin_set_super_admin_only(OPEN, ra.SuperAdminOnlyIn(on=True), SUPER)["changed"] is False
    assert targets.admin_row(world["t"][OPEN])["super_admin_only"] is True
