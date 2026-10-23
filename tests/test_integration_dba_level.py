"""The DBA level against a real database (PLA-1592).

tests/test_dba_level.py runs the rule over role rows that it builds itself.
Here the real SQL decides: `access.roles` reads `role_assignment` for the new
model, and `admins.is_dba` reads the `admins` table for the legacy one. Each
kind of caller is a row that the schema accepts, written the way the Roles
screen or the Admin scopes screen writes it.

The schema itself refuses a scoped `admin` row (migration 106). The last test
shows it, so the scope conditions in `access.is_dba` are defence in depth.

Every row uses ids and names that no real person, team or target has. The
clean-up deletes only those rows and the principal rows the mirror made for
them.
"""
import os

import psycopg
import pytest

from queryhub import access, admins, db, teams

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

TARGET = "it-dba-level-target"
TEAM = "it-dba-level-team"

# The new model: one principal per caller, with its role rows.
NEW = {  # slack id -> (name, the dba answer)
    "U0ITDBA001": ("IT DBA Super", True),
    "U0ITDBA002": ("IT DBA Capped", True),
    "U0ITDBA003": ("IT DBA Temporary", False),
    "U0ITDBA004": ("IT DBA Lapsed", False),
    "U0ITDBA005": ("IT DBA Revoked", False),
    "U0ITDBA006": ("IT DBA Disabled", False),
    "U0ITDBA007": ("IT DBA Lead", False),
    "U0ITDBA008": ("IT DBA Fleet Approver", False),
    "U0ITDBA009": ("IT DBA Nobody", False),
}

# The legacy model: `admins` and `temp_admin_grants` rows.
LEGACY = {  # slack id -> the dba answer
    "U0ITDBA101": True,     # every scope column NULL
    "U0ITDBA102": True,     # a tier ceiling
    "U0ITDBA103": False,    # one team
    "U0ITDBA104": False,    # one target
    "U0ITDBA105": False,    # an EMPTY target list: no target at all
    "U0ITDBA106": False,    # disabled
    "U0ITDBA107": False,    # a temporary grant, and no `admins` row
}
IDS = sorted([*NEW, *LEGACY])


def _wipe():
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        cur.execute("DELETE FROM temp_admin_grants WHERE slack_user_id = ANY(%s)", (IDS,))
        cur.execute("DELETE FROM admins WHERE slack_user_id = ANY(%s)", (IDS,))
        cur.execute("SELECT principal_id FROM principal_identity "
                    " WHERE provider = 'slack' AND external_id = ANY(%s)", (IDS,))
        pids = [r["principal_id"] for r in cur.fetchall()]
        for sql in ("DELETE FROM access_grant WHERE principal_id = ANY(%s)",
                    "DELETE FROM role_assignment WHERE principal_id = ANY(%s)",
                    "DELETE FROM principal_setting WHERE principal_id = ANY(%s)",
                    "DELETE FROM team_member WHERE principal_id = ANY(%s)",
                    "DELETE FROM principal_identity WHERE principal_id = ANY(%s)",
                    "DELETE FROM principal WHERE id = ANY(%s)"):
            cur.execute(sql, (pids,))
        cur.execute("DELETE FROM auth_event_outbox WHERE slack_user_id = ANY(%s)", (IDS,))
        cur.execute("DELETE FROM team WHERE name = %s", (TEAM,))
        cur.execute("DELETE FROM target_servers WHERE alias = %s", (TARGET,))


@pytest.fixture
def world():
    _wipe()
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        cur.execute("INSERT INTO target_servers (alias, host, default_database, username, "
                    "  password_encrypted, enabled) "
                    "VALUES (%s, 'it.example.internal', 'd', 'unused', 'unused', FALSE) "
                    "RETURNING id", (TARGET,))
        target = cur.fetchone()["id"]
        cur.execute("INSERT INTO team (name, display_name) VALUES (%s, 'IT DBA Team') "
                    "RETURNING id", (TEAM,))
        team = cur.fetchone()["id"]

        pid = {}
        for sid, (name, _) in NEW.items():
            cur.execute("INSERT INTO principal (kind, display_name, enabled) "
                        "VALUES ('person', %s, %s) RETURNING id",
                        (name, name != "IT DBA Disabled"))
            pid[name] = cur.fetchone()["id"]
            cur.execute("INSERT INTO principal_identity (principal_id, provider, external_id) "
                        "VALUES (%s, 'slack', %s)", (pid[name], sid))

        def role(name, role="admin", *, max_tier=None, target_id=None,
                 valid_from="NOW()", valid_until=None, revoked=False):
            cur.execute(
                "INSERT INTO role_assignment (principal_id, role, all_teams, "
                "  scope_target_id, all_targets, max_tier, any_tier, valid_from, "
                "  valid_until, revoked_at) "
                f"VALUES (%s, %s, TRUE, %s, %s, %s, %s, {valid_from}, "
                f"  {valid_until or 'NULL'}, {'NOW()' if revoked else 'NULL'})",
                (pid[name], role, target_id, target_id is None, max_tier,
                 max_tier is None))

        role("IT DBA Super")
        role("IT DBA Capped", max_tier="ro")
        role("IT DBA Temporary", valid_until="NOW() + INTERVAL '1 day'")
        role("IT DBA Lapsed", valid_from="NOW() - INTERVAL '2 days'",
             valid_until="NOW() - INTERVAL '1 day'")
        role("IT DBA Revoked", revoked=True)
        role("IT DBA Disabled")
        role("IT DBA Lead", "approver", max_tier="ro", target_id=target)
        role("IT DBA Fleet Approver", "approver")

        admin = ("INSERT INTO admins (slack_user_id, name, enabled, max_tier, "
                 "  scope_team_ids, scope_target_ids) VALUES (%s, %s, %s, %s, %s, %s)")
        cur.execute(admin, ("U0ITDBA101", "IT DBA Legacy Super", True, None, None, None))
        cur.execute(admin, ("U0ITDBA102", "IT DBA Legacy Capped", True, "ro", None, None))
        cur.execute(admin, ("U0ITDBA103", "IT DBA Legacy Team", True, None, [team], None))
        cur.execute(admin, ("U0ITDBA104", "IT DBA Legacy Target", True, None, None, [target]))
        cur.execute(admin, ("U0ITDBA105", "IT DBA Legacy Empty", True, None, None, []))
        cur.execute(admin, ("U0ITDBA106", "IT DBA Legacy Off", False, None, None, None))
        cur.execute("INSERT INTO temp_admin_grants (slack_user_id, expires_at, reason, "
                    "  granted_by) VALUES ('U0ITDBA107', NOW() + INTERVAL '1 day', "
                    "  'integration test', 'U0ITDBA101')")
    yield {"target": target, "team": team, "pid": pid}
    _wipe()


@pytest.mark.parametrize("sid", list(NEW))
def test_the_new_model_answers_from_role_assignment(world, sid):
    assert access.is_dba(sid) is NEW[sid][1], NEW[sid][0]


def test_the_new_model_through_the_switch(world, monkeypatch):
    monkeypatch.setattr(teams, "use_v2", lambda: True)
    got = {sid: admins.is_dba(sid) for sid in NEW}
    assert got == {sid: want for sid, (_, want) in NEW.items()}
    # A temporary admin is an admin, and is not a DBA.
    assert admins.is_admin("U0ITDBA003") and not admins.is_dba("U0ITDBA003")
    # A capped admin is a DBA, and is not a super-admin.
    assert admins.is_dba("U0ITDBA002") and not admins.is_super_admin("U0ITDBA002")


def test_the_legacy_model_answers_from_the_admins_table(world, monkeypatch):
    monkeypatch.setattr(teams, "use_v2", lambda: False)
    got = {sid: admins.is_dba(sid) for sid in LEGACY}
    assert got == LEGACY
    # The temporary grant makes an admin, and not a DBA.
    assert admins.is_admin("U0ITDBA107") and not admins.is_dba("U0ITDBA107")
    # The scoped ones are admins too, so the DBA check is what refuses them.
    assert admins.is_admin("U0ITDBA103") and admins.is_admin("U0ITDBA104")


@pytest.mark.parametrize("scope", ["team", "target"])
def test_the_schema_refuses_a_scoped_admin_row(world, scope):
    """`role_assignment_admin_is_fleet_wide` (migration 106): an `admin` row
    covers every team and every target. A scoped administrator is an
    `approver`."""
    name = "IT DBA Nobody"
    cols = ("scope_team_id, all_teams, all_targets" if scope == "team"
            else "scope_target_id, all_targets, all_teams")
    value = world["team"] if scope == "team" else world["target"]
    with pytest.raises(psycopg.errors.CheckViolation) as e:
        with db.transaction() as cur:
            cur.execute(f"INSERT INTO role_assignment (principal_id, role, {cols}, any_tier) "
                        "VALUES (%s, 'admin', %s, FALSE, TRUE, TRUE)",
                        (world["pid"][name], value))
    assert e.value.diag.constraint_name == "role_assignment_admin_is_fleet_wide"
