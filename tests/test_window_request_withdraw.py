"""A person withdraws their own pending auto-approve window request.

The function is one atomic UPDATE plus its audit row. The route answers 200,
404 and 409. A decision on a withdrawn row is refused with its own words.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from queryhub import auto_approve_requests as aar
from queryhub.web import routes_requests as rr

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Jordan Ray"}
ROW = {"id": 7, "requester_slack_id": "U0EXAMPLE001", "target_server_id": 3,
       "database_name": None, "max_tier": "ro", "window_minutes": 60,
       "status": "pending"}


class Cur:
    def __init__(self, row):
        self.row = row
        self.sql: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.row


class Txn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *exc):
        return False


@pytest.fixture
def wd(monkeypatch):
    st = {"cur": Cur(dict(ROW)), "audit": []}
    monkeypatch.setattr(aar.db, "transaction", lambda: Txn(st["cur"]))
    monkeypatch.setattr(aar.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((cur, action, details)))
    return st


def test_withdraw_sql_names_requester_and_pending(wd):
    assert aar.withdraw(7, "U0EXAMPLE001", "Jordan Ray")["status"] == "pending"
    sql, params = wd["cur"].sql[0]
    assert "SET status = 'withdrawn'" in sql
    assert re.search(r"WHERE id = %s AND requester_slack_id = %s "
                     r"AND status = 'pending'", sql)
    assert params == (7, "U0EXAMPLE001")
    assert "decided_by" not in sql


def test_withdraw_writes_audit_on_the_same_cursor(wd):
    aar.withdraw(7, "U0EXAMPLE001", "Jordan Ray")
    (cur, action, details), = wd["audit"]
    assert cur is wd["cur"]
    assert action == "window_request_withdrawn"
    assert details["request_id"] == 7 and details["target_server_id"] == 3
    assert details["window_minutes"] == 60


def test_withdraw_returns_none_and_no_audit_when_nothing_matched(wd):
    wd["cur"].row = None
    assert aar.withdraw(7, "U0EXAMPLE002") is None
    assert wd["audit"] == []


@pytest.fixture
def route(monkeypatch):
    st = {"row": dict(ROW), "done": dict(ROW, status="withdrawn")}
    monkeypatch.setattr(aar, "get", lambda rid: st["row"])
    monkeypatch.setattr(aar, "withdraw", lambda rid, uid, name=None: st["done"])
    return st


def test_route_200(route):
    out = rr.withdraw_window_request(7, CLAIMS)
    assert out == {"id": 7, "status": "withdrawn"}


def test_route_404_for_another_persons_request(route):
    route["row"]["requester_slack_id"] = "U0EXAMPLE002"
    with pytest.raises(HTTPException) as e:
        rr.withdraw_window_request(7, CLAIMS)
    assert e.value.status_code == 404


def test_route_404_for_unknown_request(route):
    route["row"] = None
    with pytest.raises(HTTPException) as e:
        rr.withdraw_window_request(7, CLAIMS)
    assert e.value.status_code == 404


def test_route_409_when_not_pending(route):
    route["row"]["status"] = "approved"
    with pytest.raises(HTTPException) as e:
        rr.withdraw_window_request(7, CLAIMS)
    assert e.value.status_code == 409


def test_route_409_when_the_update_loses_the_race(route):
    route["done"] = None
    with pytest.raises(HTTPException) as e:
        rr.withdraw_window_request(7, CLAIMS)
    assert e.value.status_code == 409


def test_decision_on_withdrawn_row_is_refused_with_the_withdrawn_words(monkeypatch):
    from queryhub import admins
    monkeypatch.setattr(admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(aar, "get", lambda rid: dict(ROW, status="withdrawn"))
    for approve in (True, False):
        with pytest.raises(aar.WindowDecisionRefused) as e:
            aar.decide_window(7, approve=approve, actor_id="U0EXAMPLE009",
                              actor_name="Casey Lin")
        assert e.value.status == 409
        assert e.value.message == "The person withdrew this request."


def test_migration_widens_the_check_and_is_idempotent():
    sql = (Path(__file__).resolve().parent.parent / "migrations"
           / "195_auto_approve_request_withdrawn.sql").read_text()
    assert re.search(r"DROP CONSTRAINT IF EXISTS auto_approve_requests_status_check", sql)
    assert re.search(r"ADD CONSTRAINT auto_approve_requests_status_check\s+"
                     r"CHECK \(status IN \('pending', 'approved', 'rejected', "
                     r"'withdrawn'\)\)", sql)
