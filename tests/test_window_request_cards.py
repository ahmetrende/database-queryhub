"""One decision closes every admin's copy of a window request card.

Before this, only the card that the deciding admin clicked lost its buttons.
The other admins kept Approve and Reject on a request that was already decided.
The bot now stores one row per admin card and replaces every copy on approve,
reject and withdraw, from Slack and from the web.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from queryhub import admins, auto_approve_requests as aar
from queryhub.slack_app import handlers, notifications, ro_window
from queryhub.web import routes_admin, routes_queries, routes_requests

REQ = {"id": 70, "requester_slack_id": "U0REQUESTER", "target_server_id": 3,
       "database_name": None, "max_tier": "ro", "window_minutes": 480,
       "reason": "reading the ledger", "status": "pending"}
STORED = [
    {"admin_slack_id": "U0ADMINA", "channel_id": "DA", "message_ts": "1.1"},
    {"admin_slack_id": "U0ADMINB", "channel_id": "DB", "message_ts": "2.2"},
]


@pytest.fixture
def slack(monkeypatch):
    """Fake Slack: record every card update and every post."""
    st = {"updates": [], "posts": [], "recorded": [], "fail_update_for": set(),
          "fail_post_for": set(), "stored": list(STORED)}

    def update(client, **kw):
        if kw["channel"] in st["fail_update_for"]:
            raise RuntimeError("chat.update failed")
        st["updates"].append(kw)

    def post(client, **kw):
        if kw["channel"] in st["fail_post_for"]:
            raise RuntimeError("chat.postMessage failed")
        st["posts"].append(kw)
        return {"ts": f"ts-{kw['channel']}"}

    monkeypatch.setattr(notifications, "_update", update)
    monkeypatch.setattr(notifications, "_post", post)
    monkeypatch.setattr(notifications, "dm_requester", lambda *a, **k: None)
    monkeypatch.setattr(aar, "list_admin_dms", lambda rid: st["stored"])
    monkeypatch.setattr(aar, "record_admin_dm",
                        lambda rid, admin_id, ch, ts:
                        st["recorded"].append((rid, admin_id, ch, ts)))
    return st


class FakeClient:
    def conversations_open(self, users):
        return {"channel": {"id": "D" + users[-1]}}


# --- storing the cards --------------------------------------------------------

def test_dm_admins_records_one_row_per_admin_with_the_posted_ts(slack):
    told = [{"slack_user_id": "UA"}, {"slack_user_id": "UB"}]
    assert aar._dm_admins(FakeClient(), REQ, "prod-ledger", told) == 2
    assert slack["recorded"] == [(70, "UA", "DA", "ts-DA"), (70, "UB", "DB", "ts-DB")]


def test_dm_admins_still_records_the_others_when_one_post_fails(slack):
    slack["fail_post_for"] = {"DA"}
    told = [{"slack_user_id": "UA"}, {"slack_user_id": "UB"}]
    assert aar._dm_admins(FakeClient(), REQ, "prod-ledger", told) == 1
    assert slack["recorded"] == [(70, "UB", "DB", "ts-DB")]


def test_a_recording_failure_keeps_the_count_and_the_loop(slack, monkeypatch):
    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(aar, "record_admin_dm", boom)
    told = [{"slack_user_id": "UA"}, {"slack_user_id": "UB"}]
    assert aar._dm_admins(FakeClient(), REQ, "prod-ledger", told) == 2
    assert len(slack["posts"]) == 2


def test_dm_admins_records_nothing_when_slack_is_off(slack, monkeypatch):
    monkeypatch.setattr(notifications, "_post", lambda client, **kw: None)
    assert aar._dm_admins(FakeClient(), REQ, "prod-ledger",
                          [{"slack_user_id": "UA"}]) == 1
    assert slack["recorded"] == []


# --- replacing the cards ------------------------------------------------------

def test_update_admin_cards_updates_each_stored_row_without_buttons(slack):
    ro_window.update_admin_cards(object(), 70, "closed text")
    assert [(u["channel"], u["ts"]) for u in slack["updates"]] == [("DA", "1.1"), ("DB", "2.2")]
    for u in slack["updates"]:
        assert u["text"] == "closed text"
        assert [b["type"] for b in u["blocks"]] == ["section"]


def test_one_failing_update_does_not_stop_the_next(slack):
    slack["fail_update_for"] = {"DA"}
    ro_window.update_admin_cards(object(), 70, "closed text")
    assert [u["channel"] for u in slack["updates"]] == ["DB"]


def test_decision_line_wording():
    assert ro_window.decision_line(70, "approved", "UB", REQ, "prod-ledger") == (
        ":white_check_mark: *Window #70 approved* by <@UB> — RO on `prod-ledger` for 8h.")
    assert ro_window.decision_line(70, "rejected", "UB") == (
        ":no_entry: *Window #70 rejected* by <@UB>.")
    assert ro_window.decision_line(70, "withdrawn", "UR") == (
        ":leftwards_arrow_with_hook: *Window #70 withdrawn* by the requester.")


# --- the Slack approve path (the bug) ----------------------------------------

def test_slack_approve_updates_the_other_admins_stored_card(slack, monkeypatch):
    monkeypatch.setattr(admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(aar, "decide_window",
                        lambda rid, **kw: {"request": REQ, "alias": "prod-ledger",
                                           "status": "approved"})
    body = {"user": {"id": "U0ADMINB", "name": "b"},
            "actions": [{"value": "70"}],
            "container": {"channel_id": "DB", "message_ts": "2.2"}}
    handlers.handle_ro_window_approve(lambda: None, body, object())
    channels = {u["channel"] for u in slack["updates"]}
    assert "DA" in channels                      # the other admin's card
    assert "DB" in channels                      # the clicked card
    assert all("approved" in u["text"] for u in slack["updates"])


def test_already_decided_closes_only_the_clicked_card_and_names_the_decider(
        slack, monkeypatch):
    """The other copies already say who decided. A late click on a stale card
    (one sent before migration 206) must not replace that line everywhere."""
    monkeypatch.setattr(admins, "is_admin", lambda uid: True)

    def refused(rid, **kw):
        raise aar.WindowDecisionRefused("Already approved.")
    monkeypatch.setattr(aar, "decide_window", refused)
    monkeypatch.setattr(aar, "get", lambda rid: dict(
        REQ, status="approved", decided_by_slack_id="U0ADMINB"))
    body = {"user": {"id": "U0ADMINA", "name": "a"},
            "actions": [{"value": "70"}],
            "container": {"channel_id": "DA", "message_ts": "1.1"}}
    handlers.handle_ro_window_approve(lambda: None, body, object())
    assert [u["channel"] for u in slack["updates"]] == ["DA"]
    assert "already *approved* by <@U0ADMINB>" in slack["updates"][0]["text"]


# --- the web routes -----------------------------------------------------------

def test_web_decision_updates_the_stored_cards(slack, monkeypatch):
    monkeypatch.setattr(routes_admin.admin, "require_admin", lambda claims, scope: "U0ADMINB")
    monkeypatch.setattr(aar, "decide_window",
                        lambda rid, **kw: {"request": REQ, "alias": "prod-ledger",
                                           "status": "approved", "grant_id": 5})
    monkeypatch.setattr(routes_queries, "_bot_client", lambda: object())
    body = SimpleNamespace(approve=True)
    out = routes_admin.admin_decide_window_request(70, body, claims={"sub": "U0ADMINB"})
    assert out["status"] == "approved"
    assert {u["channel"] for u in slack["updates"]} == {"DA", "DB"}
    assert all("Window #70 approved* by <@U0ADMINB>" in u["text"] for u in slack["updates"])


def test_web_withdraw_updates_the_stored_cards(slack, monkeypatch):
    monkeypatch.setattr(aar, "get", lambda rid: dict(REQ, status="pending"))
    monkeypatch.setattr(aar, "withdraw",
                        lambda rid, uid, name=None: dict(REQ, status="withdrawn"))
    monkeypatch.setattr(routes_requests, "_bot_client", lambda: object())
    out = routes_requests.withdraw_window_request(
        70, {"sub": "U0REQUESTER", "name": "Requester"})
    assert out == {"id": 70, "status": "withdrawn"}
    assert {u["channel"] for u in slack["updates"]} == {"DA", "DB"}
    assert all("withdrawn" in u["text"] for u in slack["updates"])
