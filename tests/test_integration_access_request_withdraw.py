"""Withdrawing an access request, on a real database (PLA-1592 U10).

tests/test_endpoint_request_withdraw.py pins the wiring with fakes. Only a
database shows what the SQL does:

- the CHECK of migration 160 accepts `withdrawn` and still refuses other words,
- the UPDATE changes only the requester's own pending row, and its audit row
  commits with it,
- a decision that comes second changes nothing and writes no grant,
- the withdrawn row frees the pending slot, so the same request can be filed
  again,
- the bell does not call a withdrawal a refusal.

Every row uses ids and an alias that no real person or target has, and the
tests delete only those rows. The audit log is append-only, so its rows stay
behind. Each test reads only the audit rows that come after its start.
"""
import logging
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from queryhub import access_requests, db
from queryhub.web import app as web_app
from queryhub.web import deps, routes_queries, routes_requests

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

ME = "U0ITWDRAW01"
OTHER = "U0ITWDRAW02"
ADMIN = "U0ITWDRAW09"
PEOPLE = [ME, OTHER]
ALIAS = "it-withdraw-fixture"


def _wipe():
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        cur.execute("DELETE FROM user_target_grants WHERE slack_user_id = ANY(%s)", (PEOPLE,))
        cur.execute("DELETE FROM access_requests WHERE requester_slack_id = ANY(%s)", (PEOPLE,))
        cur.execute("DELETE FROM requesters WHERE slack_user_id = ANY(%s)", (PEOPLE,))
        cur.execute("SELECT principal_id FROM principal_identity "
                    " WHERE provider = 'slack' AND external_id = ANY(%s)", (PEOPLE,))
        pids = [r["principal_id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM access_grant WHERE principal_id = ANY(%s)", (pids,))
        cur.execute("DELETE FROM principal_identity WHERE principal_id = ANY(%s)", (pids,))
        cur.execute("DELETE FROM principal WHERE id = ANY(%s)", (pids,))
        cur.execute("DELETE FROM auth_event_outbox WHERE slack_user_id = ANY(%s)", (PEOPLE,))
        cur.execute("DELETE FROM target_servers WHERE alias = %s", (ALIAS,))


@pytest.fixture
def since():
    _wipe()
    yield db.fetch_one("SELECT COALESCE(max(id), 0) AS n FROM audit_log")["n"]
    _wipe()


@pytest.fixture
def target(since):
    return db.insert_returning(
        "INSERT INTO target_servers (alias, host, default_database, username, "
        "  password_encrypted, enabled) "
        "VALUES (%s, 'fixture.example.test', 'app', 'unused', 'unused', FALSE) "
        "RETURNING id", (ALIAS,))["id"]


def _file(target_id, who=ME, what="report"):
    row = access_requests.create(
        principal_id=who, name="Jordan Ray", target_server_id=target_id,
        database_name="orders", attempted_query=f"it-withdraw:{what}",
        reason="month-end report", requested_tier="ro")
    assert row is not None
    return row["id"]


def _row(rid):
    return db.fetch_one("SELECT status, decided_at, decided_by_slack_id, "
                        "decision_reason FROM access_requests WHERE id = %s", (rid,))


def _withdrawals(since, rid):
    return db.fetch_all(
        "SELECT actor_slack_id, details FROM audit_log "
        " WHERE action = 'access_request_withdrawn' AND id > %s "
        "   AND (details->>'access_request_id')::bigint = %s", (since, rid))


def test_withdraw_closes_the_row_and_writes_its_audit_row(target, since):
    rid = _file(target)
    row = access_requests.withdraw(rid, ME, "Jordan Ray")
    assert row["status"] == "withdrawn"
    stored = _row(rid)
    assert stored["status"] == "withdrawn" and stored["decided_at"] is not None
    assert stored["decided_by_slack_id"] is None and stored["decision_reason"] is None
    audit = _withdrawals(since, rid)
    assert len(audit) == 1
    assert audit[0]["actor_slack_id"] == ME
    assert audit[0]["details"] == {"access_request_id": rid, "target_server_id": target,
                                   "database": "orders", "tier": "ro"}


def test_the_check_still_refuses_an_unknown_status(target):
    rid = _file(target)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE access_requests SET status = 'cancelled' WHERE id = %s", (rid,))
    assert _row(rid)["status"] == "pending"


def test_another_person_cannot_withdraw_it(target, since):
    rid = _file(target)
    assert access_requests.withdraw(rid, OTHER, "Sam Ellis") is None
    assert _row(rid)["status"] == "pending"
    assert _withdrawals(since, rid) == []


def test_a_decided_request_cannot_be_withdrawn(target, since):
    rid = _file(target)
    assert access_requests.decide(rid, "rejected", ADMIN, "Sam Ellis", "no") is not None
    assert access_requests.withdraw(rid, ME, "Jordan Ray") is None
    assert _row(rid)["status"] == "rejected"
    assert _withdrawals(since, rid) == []


def test_an_approval_after_the_withdrawal_changes_nothing_and_grants_nothing(target):
    """The race with an admin's Approve: whichever UPDATE comes second finds
    no pending row. An approval that loses writes no grant."""
    rid = _file(target)
    access_requests.withdraw(rid, ME, "Jordan Ray")
    assert access_requests.decide(rid, "approved", ADMIN, "Sam Ellis", None) is None
    assert _row(rid)["status"] == "withdrawn"
    assert db.fetch_one("SELECT count(*) AS n FROM user_target_grants "
                        " WHERE slack_user_id = %s AND target_server_id = %s",
                        (ME, target))["n"] == 0


def test_the_withdrawal_frees_the_pending_slot(target):
    rid = _file(target)
    assert access_requests.open_count_for(ME) == 1
    assert access_requests.create(
        principal_id=ME, name="Jordan Ray", target_server_id=target,
        database_name="orders", attempted_query="it-withdraw:report",
        reason="again", requested_tier="ro") is None          # still pending
    access_requests.withdraw(rid, ME, "Jordan Ray")
    assert access_requests.open_count_for(ME) == 0
    again = _file(target)                                    # the same request
    assert again != rid


def test_the_list_holds_only_the_requesters_rows(target):
    mine = _file(target)
    theirs = _file(target, who=OTHER)
    assert [r["id"] for r in access_requests.list_for(ME)] == [mine]
    assert [r["id"] for r in access_requests.list_for(OTHER)] == [theirs]


def test_the_bell_does_not_call_a_withdrawal_a_refusal(target):
    withdrawn = _file(target, what="a")
    refused = _file(target, what="b")
    access_requests.withdraw(withdrawn, ME, "Jordan Ray")
    access_requests.decide(refused, "rejected", ADMIN, "Sam Ellis", "no")
    items = routes_queries._derive_notifications(ME)
    ids = {i["id"] for i in items if i["kind"] == "endpoint"}
    assert ids == {f"er{refused}"}


@pytest.fixture
def client(monkeypatch):
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(routes_requests.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(routes_requests, "_bot_client", lambda: None)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": ME, "provider": "slack", "sid": "s1", "name": "Jordan Ray"}
    yield TestClient(app, raise_server_exceptions=False)
    logging.disable(logging.NOTSET)


def test_the_routes_list_withdraw_and_refuse_a_second_withdrawal(target, since, client):
    rid = _file(target)
    listed = client.get("/api/endpoint-requests").json()["requests"]
    assert [(e["id"], e["status"]) for e in listed] == [(f"er_{rid}", "submitted")]
    r = client.post(f"/api/endpoint-requests/er_{rid}/withdraw")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "withdrawn" and r.json()["decidedAt"]
    assert client.get("/api/endpoint-requests").json()["requests"][0]["status"] \
        == "withdrawn"
    again = client.post(f"/api/endpoint-requests/er_{rid}/withdraw")
    assert again.status_code == 409
    assert len(_withdrawals(since, rid)) == 1


def test_an_id_too_long_for_a_bigint_is_404_not_a_database_error(client):
    """PostgreSQL compares the number as `numeric` and finds no row. Measured
    here, because a bigint overflow would answer 500."""
    r = client.post("/api/endpoint-requests/" + "9" * 30 + "/withdraw")
    assert r.status_code == 404
