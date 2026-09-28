"""The metrics views read the access model the fleet runs on (migration 135).

Pods replaced teams on 2026-09-08 and the legacy `teams` / `team_members`
tables were emptied. Three views still read them, so every request in the
dashboard came out "(unteamed)" and the who-can-what table behind `/sql
whoami` and `/sql roles` missed every role and grant written since. These run
the views against a real database, because the bugs lived in SQL: a join to an
empty table, and a COALESCE inside a subquery that returned no row.
"""
import os

import pytest

from queryhub import db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

REQUESTER = "U0METRIC01"
CAPTAIN = "U0METRIC02"


def _principal(slack_id, name):
    row = db.fetch_one("SELECT principal_id FROM principal_identity "
                       " WHERE provider = 'slack' AND external_id = %s", (slack_id,))
    if row:
        return row["principal_id"]
    pid = db.insert_returning(
        "INSERT INTO principal (kind, display_name, enabled) VALUES ('person', %s, TRUE) "
        "RETURNING id", (name,))["id"]
    db.execute("INSERT INTO principal_identity (principal_id, provider, external_id) "
               "VALUES (%s, 'slack', %s)", (pid, slack_id))
    return pid


@pytest.fixture(scope="module")
def seeded():
    target = db.fetch_one("SELECT id FROM target_servers WHERE alias = 'metrics-fixture'")
    target_id = target["id"] if target else db.insert_returning(
        "INSERT INTO target_servers (alias, host, default_database, username, "
        " password_encrypted, enabled) VALUES ('metrics-fixture', '127.0.0.1', 'app', "
        " 'unused', 'unused', FALSE) RETURNING id", ())["id"]
    me = _principal(REQUESTER, "Jordan Ray")
    cap = _principal(CAPTAIN, "Sam Archer")
    team = db.fetch_one("SELECT id FROM team WHERE name = 'metrics-pod'")
    team_id = team["id"] if team else db.insert_returning(
        "INSERT INTO team (name, display_name, source) VALUES ('metrics-pod', "
        "'Metrics Pod', 'pod-sync') RETURNING id", ())["id"]
    if not db.fetch_one("SELECT 1 FROM team_member WHERE team_id = %s AND principal_id = %s",
                        (team_id, me)):
        db.execute("INSERT INTO team_member (team_id, principal_id) VALUES (%s, %s)",
                    (team_id, me))
    if not db.fetch_one("SELECT 1 FROM role_assignment WHERE principal_id = %s", (cap,)):
        db.execute("INSERT INTO role_assignment (principal_id, role, scope_team_id, "
                   " scope_target_id, max_tier) VALUES (%s, 'approver', %s, %s, 'ro')",
                   (cap, team_id, target_id))
    if not db.fetch_one("SELECT 1 FROM access_grant WHERE principal_id = %s", (me,)):
        db.execute("INSERT INTO access_grant (principal_id, target_id, database_name, tier) "
                   "VALUES (%s, %s, 'app', 'ro')", (me, target_id))

    def request(query, **over):
        cols = {"requester_slack_id": REQUESTER, "requester_name": "Jordan Ray",
                "target_server_id": target_id, "database_name": "app", "query": query,
                "status": "completed", "created_at": "now()"}
        cols.update(over)
        names = ", ".join(cols)
        holders = ", ".join("now()" if v == "now()" else "%s" for v in cols.values())
        return db.insert_returning(
            f"INSERT INTO requests ({names}) VALUES ({holders}) RETURNING id",
            tuple(v for v in cols.values() if v != "now()"))["id"]

    return {"request": request, "team_id": team_id}


def _fact(rid):
    return db.fetch_one("SELECT * FROM p_metrics_request_facts WHERE id = %s", (rid,))


def test_a_request_carries_its_requesters_pod(seeded):
    rid = seeded["request"]("SELECT 1")
    assert _fact(rid)["team"] == "Metrics Pod"
    usage = db.fetch_one("SELECT * FROM p_metrics_team_usage WHERE team = 'Metrics Pod'")
    assert usage and usage["total_requests"] >= 1


def test_the_tier_is_the_one_it_ran_at_then_the_first_keyword_after_comments(seeded):
    assert _fact(seeded["request"]("-- a note\nSELECT 1"))["tier"] == "ro"
    assert _fact(seeded["request"]("SELECT\n*\nFROM t"))["tier"] == "ro"
    assert _fact(seeded["request"]("/* why */ UPDATE t SET a = 1 WHERE id = 1"))["tier"] == "rw"
    ran_as = seeded["request"]("DO $$ BEGIN PERFORM 1; END $$", executed_tier="ddl")
    assert _fact(ran_as)["tier"] == "ddl_or_other"


def test_an_approver_outside_admins_keeps_their_name(seeded):
    rid = seeded["request"]("SELECT 2", decided_by_slack_id=CAPTAIN,
                            decided_by_name="sam.archer", decided_at="now()")
    fact = _fact(rid)
    assert fact["decided_by_name"] == "Sam Archer" and fact["auto_approved"] is False


def test_an_auto_approval_is_marked_and_named(seeded):
    rid = seeded["request"]("SELECT 3", decided_by_slack_id="AUTO",
                            decided_by_name="auto-approved (grant #1)", decided_at="now()")
    fact = _fact(rid)
    assert fact["auto_approved"] is True and fact["decided_by_name"] == "auto-approved"


def test_who_can_what_reads_roles_and_grants_from_the_access_model(seeded):
    me = db.fetch_one("SELECT * FROM p_metrics_who_can_what WHERE slack_user_id = %s",
                      (REQUESTER,))
    assert me["teams"] == ["Metrics Pod"]
    assert me["user_grants"] == ["metrics-fixture/app(ro)"]
    assert me["is_admin"] is False
    cap = db.fetch_one("SELECT * FROM p_metrics_who_can_what WHERE slack_user_id = %s",
                       (CAPTAIN,))
    assert cap["is_admin"] is True and cap["admin_max_tier"] == "ro"
    assert cap["admin_scope_team_ids"] == [seeded["team_id"]]
