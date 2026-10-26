"""The audit search answers with the channel and the decision facts, on a real PostgreSQL.

The hermetic tests pin what the code asks. Only a database shows that the new
SELECT columns exist, that the new UNION branch of the search term runs, and that
the channel word finds the requests of that door.

Every row is keyed to names no real target or person has. The audit log is
append-only, so its rows stay behind; the requests stay too, because the audit
rows point at them. Run this only against a scratch database.
"""
from __future__ import annotations

import os

import pytest

from queryhub import audit, db
from queryhub.web import routes_admin as ra

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

ALIAS = "it-ach-main"
SUPER_ID, ASKER, DECIDER = "U0ACHSUP01", "U0ACHASK01", "U0ACHDEC01"
PEOPLE = {SUPER_ID: "IT ACH Super", ASKER: "IT ACH Asker", DECIDER: "IT ACH Decider"}
SUPER = {"sub": SUPER_ID, "name": "IT ACH Super"}
MARK = "it_ach_marker_7731"
NOTE = "it-ach looks fine"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(ra, "_slack_profile", lambda uid: {})
    tid = (db.fetch_one("SELECT id FROM target_servers WHERE alias = %s", (ALIAS,)) or
           db.insert_returning(
               "INSERT INTO target_servers (alias, host, default_database, username, "
               "  password_encrypted, engine, enabled) "
               "VALUES (%s, 'ach.example.internal', 'app', 'unused', 'unused', 'postgres', TRUE) "
               "RETURNING id", (ALIAS,)))["id"]
    with db.transaction() as cur:
        for sid, name in PEOPLE.items():
            cur.execute("INSERT INTO requesters (slack_user_id, name, enabled) "
                        "VALUES (%s, %s, TRUE) ON CONFLICT (slack_user_id) DO NOTHING",
                        (sid, name))
        cur.execute("INSERT INTO admins (slack_user_id, name) VALUES (%s, %s) "
                    "ON CONFLICT (slack_user_id) DO NOTHING", (SUPER_ID, PEOPLE[SUPER_ID]))
    return tid


def _request(tid, origin, n, decided=False):
    """One request of a door, with a unique query text, and one audit row on it."""
    query = f"select '{MARK}_{origin}_{n}'"
    cols = ("requester_slack_id, requester_name, target_server_id, database_name, query, "
            "status, origin, required_tier, engine")
    vals = [ASKER, PEOPLE[ASKER], tid, "app", query, "completed", origin, "ro", "postgres"]
    if decided:
        cols += ", decided_by_slack_id, decided_by_name, decision_reason, decided_at"
        vals += [DECIDER, "snapshot.decider", NOTE]
    holders = ", ".join(["%s"] * len(vals)) + (", NOW()" if decided else "")
    rid = db.insert_returning(
        f"INSERT INTO requests ({cols}) VALUES ({holders}) RETURNING id", tuple(vals))["id"]
    with db.transaction() as cur:
        audit.log_in(cur, rid, DECIDER if decided else ASKER, PEOPLE[ASKER], "approved", {})
    return rid, query


def _rows(q):
    out = ra.admin_audit_search(ra.AuditSearchIn(q=q, limit=200), claims=SUPER)
    return out["rows"]


def test_each_request_row_names_its_door(world):
    made = {o: _request(world, o, 1) for o in ("slack", "web", "idp", "mcp")}
    rows = _rows(MARK + "_")
    by_req = {r["request"]["id"]: r["request"] for r in rows if r["request"]}
    want = {"slack": "Slack", "web": "Web", "idp": "IdP", "mcp": "MCP"}
    for origin, (rid, _q) in made.items():
        req = by_req[rid]
        assert req["origin"] == origin
        assert req["channel"] == want[origin]
        assert req["submittedAt"], "the submit time is read from the request row"


def test_the_decision_facts_come_from_the_request_row(world):
    rid, query = _request(world, "idp", 2, decided=True)
    req = next(r["request"] for r in _rows(query) if r["request"] and r["request"]["id"] == rid)
    assert req["decidedById"] == DECIDER
    assert req["decidedBy"] == PEOPLE[DECIDER], "the roster name wins over the snapshot"
    assert req["decisionNote"] == NOTE
    assert req["decidedAt"]


def test_an_undecided_request_has_no_decision_facts(world):
    rid, query = _request(world, "web", 3)
    req = next(r["request"] for r in _rows(query) if r["request"] and r["request"]["id"] == rid)
    assert req["decidedBy"] is None and req["decisionNote"] is None and req["decidedAt"] is None


def test_the_channel_word_finds_that_doors_requests(world):
    idp, _ = _request(world, "idp", 4)
    web, _ = _request(world, "web", 4)
    found = {r["request"]["id"] for r in _rows("idp") if r["request"]}
    assert idp in found, "q=idp must find the request that came through the IdP"
    assert web not in found, "q=idp must not find a web request"
