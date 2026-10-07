"""Deciding an access request on the QueryHub Web screen.

The screen lists what is `submitted` and decides with Reject / Provision. Two
things were wrong. The API sent the stored word (`pending`), so no request was
ever listed and none could be decided. And a decision made here changed the row
but left live Approve / Reject buttons in the admins' Slack DMs, and a
rejection never reached the requester although the screen says it did.

These tests call the route functions directly, with the database and Slack
replaced, so they check the wiring and the words.
"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub.slack_app import access
from queryhub.web import routes_admin as ra
from queryhub.web import routes_queries

CLAIMS = {"sub": "U0ADMINAAAA", "name": "Admin One"}
LOCAL = {"sub": "local:ops", "name": "Ops Person"}

PENDING = {
    "id": 17, "requester_slack_id": "U0REQUESTER", "requester_name": "dev.one",
    "target_server_id": 52, "database_name": None, "requested_tier": None,
    "attempted_query": None, "reason": "need one look", "status": "pending",
    "created_at": None, "decided_at": None,
}


@pytest.fixture
def world(monkeypatch):
    calls = SimpleNamespace(decided=[], audit=[], announced=[])

    monkeypatch.setattr(ra.admin, "require_admin",
                        lambda claims, need, **kw: claims["sub"])
    monkeypatch.setattr(ra, "_alias_of", lambda tid: "demo-orders")
    monkeypatch.setattr(
        ra.access_requests, "get",
        lambda rid: dict(PENDING) if rid == 17 else None)

    def fake_decide(rid, status, uid, name, note):
        calls.decided.append((rid, status, uid, name, note))
        row = dict(PENDING, status=status)
        if status == "approved":
            row["auto_grant"] = {"applied": True, "reason": "granted",
                                 "mode": "ro", "databases": None,
                                 "expires_at": None}
        return row

    monkeypatch.setattr(ra.access_requests, "decide", fake_decide)

    @contextmanager
    def fake_txn():
        yield object()

    monkeypatch.setattr(ra.db, "transaction", fake_txn)
    monkeypatch.setattr(
        ra.audit, "log_in",
        lambda cur, rid, aid, aname, action, details=None:
        calls.audit.append((action, details)))
    monkeypatch.setattr(routes_queries, "_bot_client", lambda: "CLIENT")
    monkeypatch.setattr(
        ra.targets, "get",
        lambda tid: SimpleNamespace(alias="demo-orders", id=tid, host="h"))
    monkeypatch.setattr(access, "access_context_md", lambda req: "CTX")
    monkeypatch.setattr(
        access, "announce_decision",
        lambda client, updated, target, status_line, dm:
        calls.announced.append((client, updated["id"], status_line, dm)))
    return calls


def _decide(approve, note=None, claims=CLAIMS):
    return ra.admin_decide_endpoint(
        17, ra.EndpointDecisionIn(approve=approve, note=note), claims)


# ---- the decision -----------------------------------------------------------

def test_approve_answers_in_the_screens_words(world):
    out = _decide(True)
    assert out["status"] == "provisioned"
    assert out["tier"] == "RO" and out["server"] == "demo-orders"
    assert world.decided == [(17, "approved", "U0ADMINAAAA", "Admin One", None)]
    assert world.audit == []        # an approval is audited inside decide()


def test_approve_retires_the_cards_and_tells_the_requester(world):
    _decide(True)
    client, rid, status_line, dm = world.announced[0]
    assert (client, rid) == ("CLIENT", 17)
    assert status_line.startswith(
        ":white_check_mark: Approved by <@U0ADMINAAAA>")
    assert "Granted automatically" in status_line
    assert "approved* by <@U0ADMINAAAA>." in dm


def test_reject_is_audited_and_announced_with_the_note(world):
    out = _decide(False, note="too broad")
    assert out["status"] == "rejected"
    assert world.audit == [("endpoint_rejected", {"request_id": 17})]
    _, _, status_line, dm = world.announced[0]
    assert status_line == ":x: Rejected by <@U0ADMINAAAA> — too broad"
    assert dm.endswith("\n*Reason:* too broad")


def test_reject_without_a_note_says_so(world):
    _decide(False)
    assert world.announced[0][2].endswith("— No reason given.")


def test_a_local_account_is_named_not_mentioned(world):
    _decide(True, claims=LOCAL)
    assert world.announced[0][2].startswith(
        ":white_check_mark: Approved by Ops Person")


def test_a_slack_failure_does_not_undo_the_answer(world, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("slack is down")

    monkeypatch.setattr(access, "announce_decision", boom)
    out = _decide(True)
    assert out["status"] == "provisioned"
    assert world.decided        # the decision stands


def test_without_slack_nobody_is_told_and_the_answer_is_the_same(world,
                                                                 monkeypatch):
    monkeypatch.setattr(routes_queries, "_bot_client", lambda: None)
    out = _decide(True)
    assert out["status"] == "provisioned"
    assert world.announced == []


def test_an_unknown_request_is_404(world):
    with pytest.raises(HTTPException) as e:
        ra.admin_decide_endpoint(99, ra.EndpointDecisionIn(approve=True), CLAIMS)
    assert e.value.status_code == 404
    assert world.decided == [] and world.announced == []


def test_a_request_that_is_decided_is_409_and_tells_nobody(world, monkeypatch):
    monkeypatch.setattr(ra.access_requests, "get",
                        lambda rid: dict(PENDING, status="approved"))
    with pytest.raises(HTTPException) as e:
        _decide(True)
    assert e.value.status_code == 409
    assert world.decided == [] and world.announced == []


def test_losing_the_race_is_409_and_tells_nobody(world, monkeypatch):
    monkeypatch.setattr(ra.access_requests, "decide", lambda *a, **kw: None)
    with pytest.raises(HTTPException) as e:
        _decide(True)
    assert e.value.status_code == 409
    assert world.announced == []


# ---- the list ---------------------------------------------------------------

@pytest.fixture
def listing(monkeypatch):
    seen = []
    monkeypatch.setattr(ra.admin, "require_admin",
                        lambda claims, need, **kw: claims["sub"])
    monkeypatch.setattr(
        ra.db, "fetch_all",
        lambda sql, params=None: seen.append((sql, params)) or [])
    monkeypatch.setattr(ra.people, "namer", lambda ids: (lambda w: w))
    return seen


@pytest.mark.parametrize("asked,stored", [
    ("submitted", "pending"),       # the screen's word
    ("provisioned", "approved"),
    ("pending", "pending"),         # the stored word still works
    ("rejected", "rejected"),
])
def test_the_filter_takes_either_vocabulary(listing, asked, stored):
    ra.admin_endpoint_requests(status=asked, claims=CLAIMS)
    assert listing[0][1] == (stored,)


def test_the_list_reads_the_requested_tier(listing):
    ra.admin_endpoint_requests(status=None, claims=CLAIMS)
    assert "requested_tier" in listing[0][0]
