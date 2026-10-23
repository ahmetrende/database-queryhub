"""PLA-1592 U1, U2, U5 and U15 against a real PostgreSQL.

The hermetic tests pin what the code asks. Only a database shows that the SQL
answers it: that an ended row is not read, that a switched-off rule is not
read, that the migration-109 mirror carries the new columns into the access
model, and that the CHECKs refuse what they say they refuse.

Every row is keyed to names no real target, team or person has, and only
those rows are removed. The audit log and the outbox are append-only, so
their rows stay behind; the tests read only the rows written after a mark.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi import HTTPException

from queryhub import access, admins, auto_approve, db, grants, pii, targets
from queryhub import teams as teams_mod
from queryhub.web import admin as web_admin
from queryhub.web import routes_admin as ra
from queryhub.web import routes_queries

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

ALIAS, TEAM = "it-exr-main", "it-exr-team"
PERSON, MEMBER, ADMIN, SUPER_ID, DBA = (
    "U0EXRPER01", "U0EXRMEM01", "U0EXRADM01", "U0EXRSUP01", "U0EXRDBA01")
PEOPLE = {PERSON: "IT EXR Person", MEMBER: "IT EXR Member", ADMIN: "IT EXR Admin",
          SUPER_ID: "IT EXR Super", DBA: "IT EXR Dba"}
SUPER = {"sub": SUPER_ID, "name": "IT EXR Super"}
REASON = "it-exr fixture"


def _ids(sql, params):
    return [r["id"] for r in db.fetch_all(sql, params)]


def _wipe():
    sids = list(PEOPLE)
    t = _ids("SELECT id FROM target_servers WHERE alias = %s", (ALIAS,))
    tm = _ids("SELECT id FROM team WHERE name = %s", (TEAM,))
    # The legacy rows first: deleting one runs the mirror for that person, and
    # the mirror must still find their principal.
    db.execute("DELETE FROM pii_masking_exemptions WHERE reason = %s", (REASON,))
    db.execute("DELETE FROM auto_approve_grants WHERE slack_user_id = ANY(%s)", (sids,))
    db.execute("DELETE FROM user_target_grants WHERE slack_user_id = ANY(%s)", (sids,))
    db.execute("DELETE FROM temp_admin_grants WHERE slack_user_id = ANY(%s)", (sids,))
    db.execute("DELETE FROM admins WHERE slack_user_id = ANY(%s)", (sids,))
    db.execute("DELETE FROM requesters WHERE slack_user_id = ANY(%s)", (sids,))
    p = _ids("SELECT principal_id AS id FROM principal_identity "
             " WHERE provider = 'slack' AND external_id = ANY(%s)", (sids,))
    db.execute("DELETE FROM access_grant WHERE principal_id = ANY(%s) OR team_id = ANY(%s) "
               "   OR target_id = ANY(%s)", (p, tm, t))
    db.execute("DELETE FROM role_assignment WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal_setting WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM team_member WHERE team_id = ANY(%s) OR principal_id = ANY(%s)",
               (tm, p))
    db.execute("DELETE FROM principal_identity WHERE principal_id = ANY(%s)", (p,))
    db.execute("DELETE FROM principal WHERE id = ANY(%s)", (p,))
    db.execute("DELETE FROM team WHERE id = ANY(%s)", (tm,))
    db.execute("DELETE FROM target_servers WHERE id = ANY(%s)", (t,))


def _pid(slack_id):
    return db.fetch_one("SELECT principal_id FROM principal_identity "
                        " WHERE provider = 'slack' AND external_id = %s",
                        (slack_id,))["principal_id"]


@pytest.fixture
def world(monkeypatch):
    _wipe()
    monkeypatch.setattr(teams_mod, "use_v2", lambda: True)
    monkeypatch.setattr(ra, "_slack_profile", lambda uid: {})
    tid = db.insert_returning(
        "INSERT INTO target_servers (alias, host, default_database, username, "
        "  password_encrypted, engine, enabled) "
        "VALUES (%s, 'exr.example.internal', 'app', 'unused', 'unused', 'postgres', TRUE) "
        "RETURNING id", (ALIAS,))["id"]
    with db.transaction() as cur:
        for sid, name in PEOPLE.items():
            # The mirror makes each principal and its Slack identity.
            cur.execute("INSERT INTO requesters (slack_user_id, name, enabled) "
                        "VALUES (%s, %s, TRUE)", (sid, name))
        # A super-admin to call the routes as, and a capped admin.
        cur.execute("INSERT INTO admins (slack_user_id, name) VALUES (%s, %s)",
                    (SUPER_ID, PEOPLE[SUPER_ID]))
        cur.execute("INSERT INTO admins (slack_user_id, name, max_tier) VALUES (%s, %s, 'ro')",
                    (ADMIN, PEOPLE[ADMIN]))
    team = db.insert_returning(
        "INSERT INTO team (name, display_name) VALUES (%s, 'IT EXR Team') RETURNING id",
        (TEAM,))["id"]
    db.execute("INSERT INTO team_member (team_id, principal_id) VALUES (%s, %s)",
               (team, _pid(MEMBER)))
    yield {"tid": tid, "team": team, "target": targets.get(tid)}
    _wipe()


def _mark(table):
    return db.fetch_one(f"SELECT coalesce(max(id), 0) AS m FROM {table}")["m"]


def _audit(action, after):
    return db.fetch_all("SELECT details FROM audit_log WHERE action = %s AND id > %s "
                        " ORDER BY id", (action, after))


def _outbox(table, after):
    return db.fetch_all("SELECT op, slack_user_id, team_id, old_row, new_row "
                        "  FROM auth_event_outbox WHERE table_name = %s AND id > %s "
                        " ORDER BY id", (table, after))


def _status(fn, *a, **kw):
    with pytest.raises(HTTPException) as e:
        fn(*a, **kw)
    return e.value.status_code


# --- U2: an auto-approve rule switched off approves nothing -------------------

def _grant_ro(world, sid, db_name="app"):
    db.execute("INSERT INTO user_target_grants (slack_user_id, target_server_id, "
               " allowed_databases, mode, granted_by) VALUES (%s, %s, %s, 'ro', %s)",
               (sid, world["tid"], [db_name], SUPER_ID))


def _live_mirrored_waivers(sid):
    return db.fetch_all(
        "SELECT id, enabled FROM access_grant WHERE principal_id = %s AND auto_approve "
        "   AND mirrored_from = 'auto_approve_grants' AND revoked_at IS NULL "
        "   AND NOT is_deleted", (_pid(sid),))


def _in_view(rule_id):
    return db.fetch_one("SELECT 1 AS x FROM v_active_auto_approve WHERE grant_id = %s",
                        (rule_id,)) is not None


def test_a_switched_off_rule_approves_nothing_until_it_is_on_again(world):
    _grant_ro(world, PERSON)
    rule = db.insert_returning(
        "INSERT INTO auto_approve_grants (slack_user_id, max_tier, target_server_id, "
        " database_name, reason, granted_by) VALUES (%s, 'ro', %s, 'app', %s, %s) "
        "RETURNING id", (PERSON, world["tid"], REASON, SUPER_ID))["id"]
    assert auto_approve.decision(PERSON, "ro", world["target"], "app")["id"] == rule
    assert _in_view(rule) and len(_live_mirrored_waivers(PERSON)) == 1
    assert access.resolve(PERSON, world["tid"], "app")["auto_tier"] == "ro"

    audit_mark, outbox_mark = _mark("audit_log"), _mark("auth_event_outbox")
    assert ra._effective_access_legacy(PERSON)[1] != []
    out = ra.admin_set_auto_grant(str(rule), ra.AutoGrantPatchIn(enabled=False), claims=SUPER)
    assert out["enabled"] is False and out["changed"] is True and out["id"] == str(rule)
    # Every reader: the decision, the view, the mirror, the resolver, the list.
    assert auto_approve.decision(PERSON, "ro", world["target"], "app") is None
    assert auto_approve.active_grants(PERSON) == []
    assert not _in_view(rule)
    assert _live_mirrored_waivers(PERSON) == []
    assert access.resolve(PERSON, world["tid"], "app")["auto_tier"] is None
    assert ra._effective_access_legacy(PERSON)[1] == []
    [listed] = [g for g in ra.admin_auto_grants(SUPER)["autoGrants"] if g["id"] == str(rule)]
    assert listed["enabled"] is False
    [d] = _audit("auto_approve_disabled", audit_mark)
    assert d["details"]["grant_id"] == rule
    [ev] = _outbox("auto_approve_grants", outbox_mark)
    assert ev["slack_user_id"] == PERSON and ev["new_row"]["enabled"] is False

    ra.admin_set_auto_grant(str(rule), ra.AutoGrantPatchIn(enabled=True), claims=SUPER)
    assert auto_approve.decision(PERSON, "ro", world["target"], "app")["id"] == rule
    assert _in_view(rule) and len(_live_mirrored_waivers(PERSON)) == 1
    assert access.resolve(PERSON, world["tid"], "app")["auto_tier"] == "ro"


def test_the_run_count_is_the_approvals_that_cite_the_rule(world):
    rule = db.insert_returning(
        "INSERT INTO auto_approve_grants (slack_user_id, max_tier, target_server_id, "
        " reason) VALUES (%s, 'ro', %s, %s) RETURNING id",
        (PERSON, world["tid"], REASON))["id"]
    with db.transaction() as cur:
        for action in ("auto_approved", "auto_approved", "submitted"):
            cur.execute("INSERT INTO audit_log (actor_slack_id, action, details) "
                        "VALUES ('AUTO', %s, %s::jsonb)",
                        (action, f'{{"grant_id": {rule}, "max_tier": "ro"}}'))
    [listed] = [g for g in ra.admin_auto_grants(SUPER)["autoGrants"] if g["id"] == str(rule)]
    assert listed["runCount"] == 2 and listed["enabled"] is True


def test_a_teams_waiver_switches_off_for_every_member(world):
    tid, team = world["tid"], world["team"]
    db.execute("INSERT INTO access_grant (team_id, target_id, database_name, tier, reason) "
               "VALUES (%s, %s, 'app', 'ro', %s)", (team, tid, REASON))
    waiver = db.insert_returning(
        "INSERT INTO access_grant (team_id, target_id, database_name, tier, auto_approve, "
        " reason) VALUES (%s, %s, 'app', 'ro', TRUE, %s) RETURNING id",
        (team, tid, REASON))["id"]
    key = f"ag:{waiver}"
    assert auto_approve.decision(MEMBER, "ro", world["target"], "app")["id"] == key
    assert access.resolve(MEMBER, tid, "app")["auto_tier"] == "ro"
    assert access.resolve_many(MEMBER, [tid])[tid]["auto_tier"] == "ro"
    assert access.resolve_databases(MEMBER, [(tid, "app")])[(tid, "app")][0]["auto_tier"] == "ro"
    assert any(r["auto_approve"] for r in ra._team_rows_v2(team)[0])

    outbox_mark = _mark("auth_event_outbox")
    ra.admin_set_auto_grant(key, ra.AutoGrantPatchIn(enabled=False), claims=SUPER)
    assert auto_approve.decision(MEMBER, "ro", world["target"], "app") is None
    assert access.resolve(MEMBER, tid, "app")["auto_tier"] is None
    assert access.resolve_many(MEMBER, [tid])[tid]["auto_tier"] is None
    decided, _row = access.resolve_databases(MEMBER, [(tid, "app")])[(tid, "app")]
    assert decided["auto_tier"] is None
    assert not any(r["auto_approve"] for r in ra._team_rows_v2(team)[0])
    [listed] = [g for g in ra.admin_auto_grants(SUPER)["autoGrants"] if g["id"] == key]
    assert listed["enabled"] is False and listed["subjectType"] == "team"
    [ev] = _outbox("access_grant", outbox_mark)
    assert ev["team_id"] == team and ev["new_row"]["enabled"] is False

    ra.admin_set_auto_grant(key, ra.AutoGrantPatchIn(enabled=True), claims=SUPER)
    assert auto_approve.decision(MEMBER, "ro", world["target"], "app")["id"] == key


def test_a_switched_off_team_waiver_covers_nothing_when_access_is_granted(world):
    """Granting the team access with auto-approve asks whether a waiver it
    holds already covers it. A switched-off one decides nothing, so it covers
    nothing: a live waiver is written and the paused one is superseded."""
    tid, team = world["tid"], world["team"]
    waiver = db.insert_returning(
        "INSERT INTO access_grant (team_id, target_id, database_name, tier, auto_approve, "
        " enabled, reason) VALUES (%s, %s, 'app', 'ro', TRUE, FALSE, %s) RETURNING id",
        (team, tid, REASON))["id"]
    out = ra.admin_create_grant(ra.GrantIn(subjectType="team", subject=TEAM,
                                           connectionId=ALIAS, databases=["app"],
                                           autoApprove=True), claims=SUPER)
    assert out["autoApprove"]["written"] == 1 and out["autoApprove"]["skipped"] == []
    assert auto_approve.decision(MEMBER, "ro", world["target"], "app")["id"] != f"ag:{waiver}"


def test_a_switched_off_waiver_is_not_warned_about(world):
    from queryhub import grant_expiry
    tid, team = world["tid"], world["team"]
    db.execute("INSERT INTO access_grant (team_id, target_id, database_name, tier, "
               " auto_approve, enabled, valid_from, valid_until, reason) "
               "VALUES (%s, %s, 'app', 'ro', TRUE, FALSE, now() - interval '1 day', "
               "        now() + interval '3 hours', %s)", (team, tid, REASON))
    assert [g for g in grant_expiry.due(24) if g.get("alias") == ALIAS] == []
    db.execute("UPDATE access_grant SET enabled = TRUE WHERE team_id = %s AND auto_approve",
               (team,))
    assert [g for g in grant_expiry.due(24) if g.get("alias") == ALIAS] != []


def test_a_rule_that_ended_is_refused(world):
    rule = db.insert_returning(
        "INSERT INTO auto_approve_grants (slack_user_id, max_tier, target_server_id, "
        " starts_at, expires_at, reason) "
        "VALUES (%s, 'ro', %s, now() - interval '2 days', now() - interval '1 day', %s) "
        "RETURNING id", (PERSON, world["tid"], REASON))["id"]
    assert _status(ra.admin_set_auto_grant, str(rule), ra.AutoGrantPatchIn(enabled=False),
                   claims=SUPER) == 409
    waiver = db.insert_returning(
        "INSERT INTO access_grant (team_id, target_id, database_name, tier, auto_approve, "
        " valid_from, valid_until, reason) "
        "VALUES (%s, %s, 'app', 'ro', TRUE, now() - interval '2 days', "
        "        now() - interval '1 day', %s) RETURNING id",
        (world["team"], world["tid"], REASON))["id"]
    assert _status(ra.admin_set_auto_grant, f"ag:{waiver}", ra.AutoGrantPatchIn(enabled=False),
                   claims=SUPER) == 409


def test_only_a_waiver_can_be_switched_off(world):
    gid = db.insert_returning(
        "INSERT INTO access_grant (team_id, target_id, database_name, tier, reason) "
        "VALUES (%s, %s, 'app', 'ro', %s) RETURNING id",
        (world["team"], world["tid"], REASON))["id"]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE access_grant SET enabled = FALSE WHERE id = %s", (gid,))


def test_the_audit_search_reads_every_grant_id_shape(world):
    """The new audit rows carry ids that are not numbers: `ag:<id>` for a team
    waiver, `u:<id>:<target>` for a grant. The search joins the numeric ones
    to their rule, and a cast the planner ran before the regex once failed
    the whole page on the others."""
    with db.transaction() as cur:
        for gid in ("ag:7", f"u:{PERSON}:{world['tid']}", f"t:3:{world['tid']}", "41"):
            cur.execute("INSERT INTO audit_log (actor_slack_id, action, details) "
                        "VALUES ('AUTO', 'auto_approved', %s::jsonb)",
                        (f'{{"grant_id": "{gid}", "max_tier": "ro"}}',))
    out = ra.admin_audit_search(ra.AuditSearchIn(), claims=SUPER)
    assert out["total"] >= 4


def test_a_switched_off_rule_is_not_copied_to_a_colleague(world):
    _grant_ro(world, PERSON)
    db.execute("INSERT INTO auto_approve_grants (slack_user_id, max_tier, target_server_id, "
               " reason, enabled) VALUES (%s, 'ro', %s, %s, FALSE)",
               (PERSON, world["tid"], REASON))
    preview = ra.admin_copy_access(MEMBER, ra.CopyAccessIn(
        source=PERSON, includeTeams=False, includeAutoApprove=True, dryRun=True),
        claims=SUPER)
    assert preview["wouldCopyAutoApprove"] == 0
    out = ra.admin_copy_access(MEMBER, ra.CopyAccessIn(source=PERSON, includeTeams=False,
                                                       includeAutoApprove=True),
                               claims=SUPER)
    assert out["autoApproveCopied"] == 0
    assert db.fetch_one("SELECT count(*) AS n FROM auto_approve_grants "
                        " WHERE slack_user_id = %s", (MEMBER,))["n"] == 0


# --- U5: an admin scope ends --------------------------------------------------

def _ro_request(tid):
    return {"required_tier": "ro", "target_server_id": tid,
            "requester_slack_id": PERSON, "query": "SELECT 1"}


def _role(sid):
    return db.fetch_all("SELECT role, valid_until, revoked_at FROM role_assignment "
                        " WHERE principal_id = %s AND mirrored_from = 'admins' "
                        " ORDER BY id", (_pid(sid),))


def test_a_scope_with_an_end_reaches_every_reader_and_stops(world):
    end = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    out = ra.admin_create_scope(ra.ScopeIn(admin=DBA, role="dba", canApprove=["RO"],
                                           expiresAt=end.isoformat()), claims=SUPER)
    assert out["expiresAt"] == end.isoformat()
    [role] = _role(DBA)
    assert role["valid_until"] == end and role["revoked_at"] is None
    assert access.is_admin(DBA) and admins.can_approve(DBA, _ro_request(world["tid"]))
    # Temporary, so not the DBA level (access.is_dba needs no valid_until).
    assert not admins.is_dba(DBA)
    [listed] = [s for s in ra.admin_scopes(SUPER)["scopes"] if s["id"] == DBA]
    assert listed["expiresAt"] == end.isoformat() and listed["source"] == "temp"

    # The end passes, and the next write for that person reaches the mirror.
    outbox_mark = _mark("auth_event_outbox")
    db.execute("UPDATE admins SET expires_at = now() - interval '1 minute' "
               " WHERE slack_user_id = %s", (DBA,))
    assert [r["revoked_at"] is not None for r in _role(DBA)] == [True]
    assert not access.is_admin(DBA) and access.roles(DBA) == []
    assert not admins.can_approve(DBA, _ro_request(world["tid"]))
    assert DBA not in {a["slack_user_id"] for a in admins.list_active()}
    assert DBA not in {s["id"] for s in ra.admin_scopes(SUPER)["scopes"]}
    assert web_admin.admin_block(DBA) is None
    # Migration 147 put expires_at on the trigger's column list.
    [ev] = _outbox("admins", outbox_mark)
    assert ev["slack_user_id"] == DBA and ev["new_row"]["expires_at"] is not None


def test_a_role_whose_end_passes_with_no_write_decides_nothing(world):
    """Time passing is not a write, so no mirror runs. The role row keeps a
    valid_until in the past, and every reader of role_assignment skips it."""
    end = datetime.now(timezone.utc) + timedelta(days=3)
    ra.admin_create_scope(ra.ScopeIn(admin=DBA, canApprove=["RO"],
                                     expiresAt=end.isoformat()), claims=SUPER)
    db.execute("UPDATE role_assignment SET valid_from = now() - interval '2 days', "
               "       valid_until = now() - interval '1 day' "
               " WHERE principal_id = %s AND mirrored_from = 'admins'", (_pid(DBA),))
    assert not access.is_admin(DBA)
    assert not admins.can_approve(DBA, _ro_request(world["tid"]))
    assert DBA not in {a["slack_user_id"] for a in admins.list_active()}


def test_an_edit_without_the_field_keeps_the_end_and_null_clears_it(world):
    end = (datetime.now(timezone.utc) + timedelta(days=5)).replace(microsecond=0)
    ra.admin_create_scope(ra.ScopeIn(admin=DBA, canApprove=["RO"],
                                     expiresAt=end.isoformat()), claims=SUPER)
    ra.admin_update_scope(DBA, ra.ScopeIn(canApprove=["RO", "RW"]), claims=SUPER)
    row = db.fetch_one("SELECT expires_at, max_tier FROM admins WHERE slack_user_id = %s",
                       (DBA,))
    assert row["expires_at"] == end and row["max_tier"] == "rw"
    assert [r["valid_until"] for r in _role(DBA) if r["revoked_at"] is None] == [end]

    ra.admin_update_scope(DBA, ra.ScopeIn(canApprove=["RO"], expiresAt=None), claims=SUPER)
    assert db.fetch_one("SELECT expires_at FROM admins WHERE slack_user_id = %s",
                        (DBA,))["expires_at"] is None
    assert [r["valid_until"] for r in _role(DBA) if r["revoked_at"] is None] == [None]


def test_a_super_admin_never_ends(world):
    end = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    assert _status(ra.admin_create_scope, ra.ScopeIn(admin=DBA, role="super", expiresAt=end),
                   claims=SUPER) == 400
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO admins (slack_user_id, expires_at) "
                   "VALUES (%s, now() + interval '1 day')", (DBA,))


def test_the_legacy_readers_skip_an_ended_scope(world, monkeypatch):
    monkeypatch.setattr(teams_mod, "use_v2", lambda: False)
    db.execute("INSERT INTO admins (slack_user_id, max_tier, can_grant, expires_at) "
               "VALUES (%s, 'rw', TRUE, now() + interval '1 day')", (DBA,))
    req = _ro_request(world["tid"])
    assert admins.is_admin(DBA) and admins.can_approve(DBA, req)
    # An admin scope with an end is temporary, so it is not the DBA level.
    assert not admins.is_dba(DBA)
    assert grants.authz(DBA) == {"super": False, "max_tier": "rw"}
    assert DBA in {a["slack_user_id"] for a in admins.list_active()}
    approvers = ra._super_approvers()
    assert ra._effective_access_legacy(DBA)[2]["enabled"] is True

    db.execute("UPDATE admins SET expires_at = now() - interval '1 minute' "
               " WHERE slack_user_id = %s", (DBA,))
    assert not admins.is_admin(DBA)
    assert not admins.can_approve(DBA, req)
    assert admins._candidate_scope_rows(DBA) == []
    assert grants.authz(DBA) is None
    assert DBA not in {a["slack_user_id"] for a in admins.list_active()}
    assert admins.get_scope(DBA)["permanent"]["ended"] is True
    assert ra._super_approvers() == approvers - 1
    assert ra._effective_access_legacy(DBA)[2]["enabled"] is False
    # A temporary grant still counts once the permanent row has ended: the
    # ended row no longer outranks it in the list.
    db.execute("INSERT INTO temp_admin_grants (slack_user_id, max_tier, expires_at, "
               " reason, granted_by) VALUES (%s, 'ro', now() + interval '1 day', %s, %s)",
               (DBA, REASON, SUPER_ID))
    [row] = [a for a in admins.list_active() if a["slack_user_id"] == DBA]
    assert row["source"] == "temp"
    db.execute("DELETE FROM temp_admin_grants WHERE slack_user_id = %s", (DBA,))


def test_a_stale_end_on_a_disabled_scope_does_not_carry_over(world):
    """A body without expiresAt keeps only a LIVE end. A disabled row's old
    end must not make the scope written now dead on arrival, or worse, end it
    on a date somebody chose for a different grant."""
    db.execute("INSERT INTO admins (slack_user_id, max_tier, enabled, expires_at) "
               "VALUES (%s, 'ro', FALSE, now() + interval '2 days')", (DBA,))
    ra.admin_create_scope(ra.ScopeIn(admin=DBA, canApprove=["RO"]), claims=SUPER)
    assert db.fetch_one("SELECT expires_at, enabled FROM admins WHERE slack_user_id = %s",
                        (DBA,)) == {"expires_at": None, "enabled": True}


# --- U1: a grant's end moves in place ----------------------------------------

def _person_grant(world, ends_in):
    db.execute("INSERT INTO user_target_grants (slack_user_id, target_server_id, "
               " allowed_databases, mode, granted_by, granted_at, expires_at) "
               "VALUES (%s, %s, ARRAY['app'], 'ro', %s, now() - interval '2 days', "
               "        now() + %s)", (PERSON, world["tid"], SUPER_ID, ends_in))


def _mirrored_end():
    return db.fetch_one(
        "SELECT valid_until FROM access_grant WHERE principal_id = %s "
        "   AND mirrored_from = 'user_target_grants' AND NOT auto_approve "
        "   AND revoked_at IS NULL", (_pid(PERSON),))["valid_until"]


def test_extending_a_persons_grant_reaches_the_access_model(world):
    _person_grant(world, timedelta(days=1))
    gid = f"u:{PERSON}:{world['tid']}"
    new_end = (datetime.now(timezone.utc) + timedelta(days=10)).replace(microsecond=0)
    audit_mark, outbox_mark = _mark("audit_log"), _mark("auth_event_outbox")
    out = ra.admin_extend_grant(gid, ra.GrantPatchIn(expiresAt=new_end.isoformat(),
                                                     reason="quarter close"), claims=SUPER)
    assert out["id"] == gid and out["expiresAt"] == new_end.isoformat()
    assert db.fetch_one("SELECT expires_at FROM user_target_grants WHERE slack_user_id = %s",
                        (PERSON,))["expires_at"] == new_end
    assert _mirrored_end() == new_end
    _res, row = access.resolve_databases(PERSON, [(world["tid"], "app")])[(world["tid"], "app")]
    assert row["valid_until"] == new_end
    [d] = _audit("grant_expiry_changed", audit_mark)
    assert d["details"]["note"].startswith(f"Extended {gid} from ")
    [ev] = _outbox("user_target_grants", outbox_mark)
    assert ev["slack_user_id"] == PERSON and ev["op"] == "UPDATE"


def test_an_ended_grant_is_not_extended(world):
    _person_grant(world, timedelta(days=1))
    db.execute("UPDATE user_target_grants SET expires_at = now() - interval '1 hour' "
               " WHERE slack_user_id = %s", (PERSON,))
    assert access.resolve(PERSON, world["tid"], "app") is None
    gid = f"u:{PERSON}:{world['tid']}"
    later = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    assert _status(ra.admin_extend_grant, gid, ra.GrantPatchIn(expiresAt=later),
                   claims=SUPER) == 409
    assert access.resolve(PERSON, world["tid"], "app") is None


def test_extending_a_teams_grant_moves_every_database(world):
    tid, team = world["tid"], world["team"]
    for name in ("app", "ledger"):
        db.execute("INSERT INTO access_grant (team_id, target_id, database_name, tier, "
                   " valid_from, valid_until, reason) "
                   "VALUES (%s, %s, %s, 'ro', now() - interval '1 day', "
                   "        now() + interval '1 day', %s)", (team, tid, name, REASON))
    new_end = (datetime.now(timezone.utc) + timedelta(days=9)).replace(microsecond=0)
    outbox_mark = _mark("auth_event_outbox")
    out = ra.admin_extend_grant(f"t:{team}:{tid}", ra.GrantPatchIn(
        expiresAt=new_end.isoformat()), claims=SUPER)
    assert out["expiresAt"] == new_end.isoformat()
    ends = {r["valid_until"] for r in db.fetch_all(
        "SELECT valid_until FROM access_grant WHERE team_id = %s AND NOT auto_approve "
        "   AND revoked_at IS NULL", (team,))}
    assert ends == {new_end}
    assert access.resolve(MEMBER, tid, "ledger")["tier"] == "ro"
    assert len(_outbox("access_grant", outbox_mark)) == 2


# --- U15: an exemption's end and audience ------------------------------------

def _exempt(world, **kw):
    cols = {"target_server_id": world["tid"], "database_name": "app",
            "column_name": "email", "reason": REASON, "created_by": SUPER_ID, **kw}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    return db.insert_returning(
        f"INSERT INTO pii_masking_exemptions ({names}) VALUES ({marks}) RETURNING id",
        tuple(cols.values()))["id"]


def _cols(world, reader):
    """The columns this file's rows exempt for the reader. Fleet rows from the
    migrations (the dba toolkit row of 090 names no column) are left out."""
    return [r["column_name"] for r in pii._load_exemptions(world["tid"], "app", reader)
            if r["column_name"]]


def test_an_ended_exemption_is_not_applied(world):
    eid = _exempt(world, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    assert _cols(world, None) == ["email"]
    db.execute("UPDATE pii_masking_exemptions SET expires_at = now() - interval '1 minute' "
               " WHERE id = %s", (eid,))
    assert _cols(world, None) == []
    assert pii.exemption_decision(world["tid"], "app", "SELECT email FROM users",
                                  ["email"]) == (False, set())


def test_each_audience_reaches_its_readers_only(world):
    _exempt(world, column_name="dba_col", audience="dba")
    _exempt(world, column_name="team_col", audience="team", audience_team_id=world["team"])
    _exempt(world, column_name="super_col", audience="super", super_admin_only=True)
    assert sorted(_cols(world, ADMIN)) == ["dba_col"]
    assert sorted(_cols(world, SUPER_ID)) == ["dba_col", "super_col"]
    assert sorted(_cols(world, MEMBER)) == ["team_col"]
    assert _cols(world, PERSON) == [] and _cols(world, None) == []


def test_the_header_dots_follow_the_audience(world):
    _exempt(world, audience="dba")
    req = {"id": 1, "target_server_id": world["tid"], "database_name": "app",
           "query": "SELECT email FROM users"}
    assert routes_queries._masked_pii_cols({**req, "requester_slack_id": ADMIN},
                                           ["email"]) == []
    assert routes_queries._masked_pii_cols({**req, "requester_slack_id": PERSON},
                                           ["email"]) == ["email"]


def test_the_preview_shows_today_as_an_ordinary_reader_sees_it(world):
    _exempt(world, audience="dba")
    before, after = ra._mask_preview_pair(world["tid"], "app", None, "users", "email",
                                          ["email"], ["casey@example.com"], strength="full")
    assert before != ["casey@example.com"] and after == ["casey@example.com"]


def test_the_admin_api_round_trip(world):
    end = (datetime.now(timezone.utc) + timedelta(days=4)).replace(microsecond=0)
    body = {"connectionId": ALIAS, "databaseId": "app", "schema": "public",
            "table": "users", "column": "email", "reason": REASON,
            "audience": {"kind": "team", "teamId": world["team"]},
            "expiresAt": end.isoformat()}
    eid = int(ra.admin_add_mask_exemption(ra.MaskExemptionIn(**body), claims=SUPER)["id"])
    stored = db.fetch_one("SELECT audience, audience_team_id, super_admin_only, expires_at "
                          "  FROM pii_masking_exemptions WHERE id = %s", (eid,))
    assert stored == {"audience": "team", "audience_team_id": world["team"],
                      "super_admin_only": False, "expires_at": end}
    [row] = [e for e in ra.admin_mask_exemptions(SUPER)["exemptions"] if e["id"] == str(eid)]
    assert (row["audience"], row["audienceTeamId"], row["audienceTeamName"]) == \
        ("team", str(world["team"]), "IT EXR Team")
    assert row["expiresAt"] == end.isoformat() and row["expired"] is False

    # One row per reach (uq_pii_masking_exemptions_scope): another audience on
    # the same reach is an Edit of this row, and the refusal says so.
    assert _status(ra.admin_add_mask_exemption, ra.MaskExemptionIn(
        **{**body, "audience": "dba"}), claims=SUPER) == 409

    # The current screen echoes "team" back on save: nothing changes.
    same = ra.admin_set_mask_exemption(eid, ra.MaskPatchIn(audience="team"), claims=SUPER)
    assert same["changed"] is False
    cleared = ra.admin_set_mask_exemption(eid, ra.MaskPatchIn(expiresAt=None), claims=SUPER)
    assert cleared["changed"] is True and cleared["expiresAt"] is None


def test_migration_149_carried_the_old_flag_and_told_nobody(world):
    """The fleet-wide dba toolkit row (migration 090) had only the flag. It is
    `super` in the new column too, and the carry-over sent no DM: it changed
    no row's meaning."""
    [row] = db.fetch_all(
        "SELECT audience, super_admin_only FROM pii_masking_exemptions "
        " WHERE target_server_id IS NULL AND schema_name = 'dba' "
        "   AND table_name IS NULL AND column_name IS NULL")
    assert row == {"audience": "super", "super_admin_only": True}
    assert db.fetch_all(
        "SELECT 1 FROM auth_event_outbox WHERE table_name = 'pii_masking_exemptions' "
        "   AND old_row->>'audience' = 'everyone' AND new_row->>'audience' = 'super' "
        "   AND (old_row->>'super_admin_only')::boolean") == []


def test_the_checks_refuse_an_audience_that_names_nobody(world):
    with pytest.raises(psycopg.errors.CheckViolation):
        _exempt(world, audience="contractors")
    with pytest.raises(psycopg.errors.CheckViolation):
        _exempt(world, audience="team")
    with pytest.raises(psycopg.errors.CheckViolation):
        _exempt(world, audience="dba", audience_team_id=world["team"])
