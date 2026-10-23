"""A requester's own access requests (PLA-1592 U10).

A developer could file an access request on the web, but could not see it
again or take it back. The state reached them only as a Slack DM.

- `GET /endpoint-requests` lists the caller's own requests: the status, the
  decision note, who decided and when.
- `POST /endpoint-requests/{id}/withdraw` withdraws a pending one. A decided
  request answers 409, and the id of another person answers 404.
- The withdrawal follows the web withdraw of a SQL request: the status change
  and its audit row commit in one transaction, then the admins' Slack cards
  lose their buttons.
- `withdrawn` is a new status. Every reader of `access_requests.status` is
  checked here: the bell, the admin list, the admin decision and the audit
  screen. tests/test_integration_access_request_withdraw.py runs the SQL.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from queryhub import access_requests
from queryhub.slack_app import access as slack_access
from queryhub.web import app as web_app
from queryhub.web import deps, mapping, routes_admin, routes_queries, routes_requests

ME = "U0EXAMPLE001"
OTHER = "U0EXAMPLE002"
ADMIN = "U0EXAMPLE009"
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
CLAIMS = {"sub": ME, "provider": "slack", "sid": "s1", "name": "Jordan Ray"}


def _ar(**kw):
    row = {"id": 17, "requester_slack_id": ME, "requester_name": "jordan.ray",
           "target_server_id": 7, "database_name": "orders", "attempted_query": None,
           "reason": "[requested tier: RO] month-end report", "requested_tier": "ro",
           "status": "pending", "decided_by_slack_id": None, "decided_by_name": None,
           "decision_reason": None, "created_at": NOW, "decided_at": None}
    row.update(kw)
    return row


@pytest.fixture
def web(monkeypatch):
    logging.disable(logging.CRITICAL)
    state = SimpleNamespace(rows={17: _ar()}, listed=[], withdrawn=[], cards=[],
                            whitelisted=True)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: dict(CLAIMS)
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: state.whitelisted)
    monkeypatch.setattr(deps.requesters, "is_allowed", lambda uid: state.whitelisted)
    monkeypatch.setattr(routes_requests.access_requests, "get",
                        lambda rid: dict(state.rows[rid]) if rid in state.rows else None)

    def list_for(uid):
        state.listed.append(uid)
        return [dict(r) for r in state.rows.values() if r["requester_slack_id"] == uid]

    def withdraw(rid, uid, name):
        state.withdrawn.append((rid, uid, name))
        row = state.rows[rid]
        if row["requester_slack_id"] != uid or row["status"] != "pending":
            return None
        row.update(status="withdrawn", decided_at=NOW)
        return dict(row)

    monkeypatch.setattr(routes_requests.access_requests, "list_for", list_for)
    monkeypatch.setattr(routes_requests.access_requests, "withdraw", withdraw)
    monkeypatch.setattr(routes_requests, "_alias_of", lambda tid: "prod-main")
    monkeypatch.setattr(routes_requests.people, "namer",
                        lambda keys: lambda k: {"sam.ellis": "Sam Ellis"}.get(k, k))
    monkeypatch.setattr(routes_requests, "_bot_client", lambda: "CLIENT")
    monkeypatch.setattr(routes_requests.targets, "get",
                        lambda tid: SimpleNamespace(id=tid, alias="prod-main"))
    monkeypatch.setattr(slack_access, "update_admin_cards",
                        lambda client, rid, target, line:
                        state.cards.append((client, rid, target.alias, line)))
    state.client = TestClient(app)
    yield state
    logging.disable(logging.NOTSET)


# ---- GET /endpoint-requests ---------------------------------------------------

def test_the_list_holds_only_the_callers_requests(web):
    web.rows[18] = _ar(id=18, requester_slack_id=OTHER, requester_name="sam.ellis")
    r = web.client.get("/api/endpoint-requests")
    assert r.status_code == 200, r.text
    assert web.listed == [ME]
    assert [e["id"] for e in r.json()["requests"]] == ["er_17"]


def test_a_pending_request_has_no_decision_yet(web):
    entry = web.client.get("/api/endpoint-requests").json()["requests"][0]
    assert entry == {
        "id": "er_17", "requester": "jordan.ray", "requesterId": ME,
        "server": "prod-main", "database": "orders", "tier": "RO",
        "reason": "[requested tier: RO] month-end report", "status": "submitted",
        "requestedAt": NOW.isoformat(),
        "decidedAt": None, "decidedBy": None, "decisionNote": None,
    }


def test_a_decided_request_names_the_decider_and_keeps_the_note(web):
    web.rows[17] = _ar(status="rejected", decided_by_slack_id=ADMIN,
                       decided_by_name="sam.ellis", decision_reason="Use the replica.",
                       decided_at=NOW)
    entry = web.client.get("/api/endpoint-requests").json()["requests"][0]
    assert (entry["status"], entry["decidedBy"], entry["decisionNote"],
            entry["decidedAt"]) == ("rejected", "Sam Ellis", "Use the replica.",
                                    NOW.isoformat())


def test_a_withdrawn_request_reads_withdrawn_and_names_no_decider(web):
    web.rows[17] = _ar(status="withdrawn", decided_at=NOW)
    entry = web.client.get("/api/endpoint-requests").json()["requests"][0]
    assert (entry["status"], entry["decidedBy"], entry["decisionNote"],
            entry["decidedAt"]) == ("withdrawn", None, None, NOW.isoformat())


def test_the_list_needs_a_whitelisted_caller(web):
    web.whitelisted = False
    r = web.client.get("/api/endpoint-requests")
    assert r.status_code == 403
    assert web.listed == []


# ---- POST /endpoint-requests/{id}/withdraw --------------------------------------

@pytest.mark.parametrize("raw", ["er_17", "17"])
def test_withdraw_closes_the_request_and_retires_the_cards(web, raw):
    r = web.client.post(f"/api/endpoint-requests/{raw}/withdraw")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["id"], body["status"], body["decidedBy"]) == ("er_17", "withdrawn", None)
    assert web.withdrawn == [(17, ME, "Jordan Ray")]
    assert web.cards == [("CLIENT", 17, "prod-main",
                          ":wastebasket: Jordan Ray withdrew this request. "
                          "You do not need to act.")]


def test_the_request_of_another_person_is_404_and_changes_nothing(web):
    web.rows[17] = _ar(requester_slack_id=OTHER)
    r = web.client.post("/api/endpoint-requests/er_17/withdraw")
    assert r.status_code == 404
    assert web.withdrawn == [] and web.cards == []


@pytest.mark.parametrize("raw", ["er_99", "99", "abc", "er_", "er_-1", "9" * 30,
                                 "%C2%B2"])
def test_an_unknown_or_malformed_id_is_404(web, raw):
    r = web.client.post(f"/api/endpoint-requests/{raw}/withdraw")
    assert r.status_code == 404
    assert web.withdrawn == []


def test_a_non_ascii_digit_is_not_an_id(web):
    # `int()` reads an Arabic-Indic digit, so "١" would be request 1. And a
    # superscript two passes `isdigit()` but `int()` raises on it (a 500).
    web.rows[1] = _ar(id=1)
    r = web.client.post("/api/endpoint-requests/%D9%A1/withdraw")
    assert r.status_code == 404
    assert web.withdrawn == []


@pytest.mark.parametrize("status,words", [
    ("approved", "already has a decision"),
    ("rejected", "already has a decision"),
    ("withdrawn", "already withdrew"),
])
def test_a_request_that_is_not_pending_is_409(web, status, words):
    web.rows[17] = _ar(status=status, decided_at=NOW)
    r = web.client.post("/api/endpoint-requests/er_17/withdraw")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    assert words in r.json()["error"]["message"]
    assert web.withdrawn == [] and web.cards == []


def test_losing_the_race_to_an_admin_is_409_and_retires_nothing(web, monkeypatch):
    def admin_won(rid, uid, name):
        web.rows[rid].update(status="approved")
        return None
    monkeypatch.setattr(routes_requests.access_requests, "withdraw", admin_won)
    r = web.client.post("/api/endpoint-requests/er_17/withdraw")
    assert r.status_code == 409
    assert "already has a decision" in r.json()["error"]["message"]
    assert web.cards == []


def test_a_slack_failure_does_not_undo_the_withdrawal(web, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("slack is down")
    monkeypatch.setattr(slack_access, "update_admin_cards", boom)
    r = web.client.post("/api/endpoint-requests/er_17/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"


def test_without_slack_nobody_is_told_and_the_answer_is_the_same(web, monkeypatch):
    monkeypatch.setattr(routes_requests, "_bot_client", lambda: None)
    r = web.client.post("/api/endpoint-requests/er_17/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"
    assert web.cards == []


def test_withdraw_needs_a_whitelisted_caller(web):
    web.whitelisted = False
    assert web.client.post("/api/endpoint-requests/er_17/withdraw").status_code == 403
    assert web.withdrawn == []


# ---- access_requests.withdraw ----------------------------------------------------

class _Cursor:
    def __init__(self, row):
        self.row, self.sql = row, []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


@pytest.fixture
def txn(monkeypatch):
    holder = SimpleNamespace(cursor=None, audit=[])

    @contextmanager
    def transaction():
        yield holder.cursor
    monkeypatch.setattr(access_requests.db, "transaction", transaction)
    monkeypatch.setattr(access_requests.audit, "log_in",
                        lambda cur, rid, aid, aname, action, details=None:
                        holder.audit.append((cur, rid, aid, aname, action, details)))
    return holder


def test_the_update_names_the_requester_and_pending(txn):
    txn.cursor = _Cursor(_ar(status="withdrawn", decided_at=NOW))
    row = access_requests.withdraw(17, ME, "Jordan Ray")
    assert row["status"] == "withdrawn"
    (sql, params), = txn.cursor.sql
    assert sql.startswith("UPDATE access_requests SET status = 'withdrawn', "
                          "decided_at = NOW() WHERE id = %s AND requester_slack_id = %s "
                          "AND status = 'pending'")
    assert params == (17, ME)


def test_the_audit_row_goes_on_the_cursor_of_the_update(txn):
    txn.cursor = _Cursor(_ar(status="withdrawn", decided_at=NOW))
    access_requests.withdraw(17, ME, "Jordan Ray")
    assert txn.audit == [(txn.cursor, None, ME, "Jordan Ray", "access_request_withdrawn",
                          {"access_request_id": 17, "target_server_id": 7,
                           "database": "orders", "tier": "ro"})]


def test_a_withdrawal_that_changes_nothing_writes_no_audit_row(txn):
    txn.cursor = _Cursor(None)
    assert access_requests.withdraw(17, ME, "Jordan Ray") is None
    assert txn.audit == []


# ---- every reader of the new status ----------------------------------------------

def test_the_bell_reports_only_an_admins_decision(monkeypatch):
    """A withdrawn request has a `decided_at`. Without the status test the bell
    told its requester "An admin declined your access request"."""
    seen = []
    monkeypatch.setattr(routes_queries.db, "fetch_all",
                        lambda sql, p=None: seen.append(" ".join(sql.split())) or [])
    routes_queries._derive_notifications(ME)
    access_sql = [s for s in seen if "FROM access_requests" in s]
    assert len(access_sql) == 1
    assert "e.status IN ('approved', 'rejected')" in access_sql[0]


def test_the_admin_list_shows_a_withdrawn_request_as_withdrawn(monkeypatch):
    assert mapping.endpoint_request_entry(
        _ar(status="withdrawn"), lambda tid: "prod-main")["status"] == "withdrawn"
    seen = []
    monkeypatch.setattr(routes_admin.admin, "require_admin",
                        lambda claims, need, **kw: claims["sub"])
    monkeypatch.setattr(routes_admin.db, "fetch_all",
                        lambda sql, p=None: seen.append(p) or [])
    monkeypatch.setattr(routes_admin.people, "namer", lambda keys: lambda k: k)
    routes_admin.admin_endpoint_requests(status="withdrawn", claims={"sub": ADMIN})
    assert seen == [("withdrawn",)]


def test_an_admin_cannot_decide_a_withdrawn_request(monkeypatch):
    decided = []
    monkeypatch.setattr(routes_admin.admin, "require_admin",
                        lambda claims, need, **kw: claims["sub"])
    monkeypatch.setattr(routes_admin.access_requests, "get",
                        lambda rid: _ar(status="withdrawn", decided_at=NOW))
    monkeypatch.setattr(routes_admin.access_requests, "decide",
                        lambda *a, **k: decided.append(a))
    with pytest.raises(HTTPException) as e:
        routes_admin.admin_decide_endpoint(
            17, routes_admin.EndpointDecisionIn(approve=True), {"sub": ADMIN})
    assert e.value.status_code == 409
    assert decided == []


def test_the_withdrawal_shows_on_the_audit_screen():
    assert "access_request_withdrawn" not in mapping.AUDIT_EXCLUDE
    assert mapping.audit_kind("access_request_withdrawn") == "reject"
