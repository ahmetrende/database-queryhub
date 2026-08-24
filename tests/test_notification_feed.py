"""The bell reports decisions the reader did not make.

A super-admin approves their own submissions by definition, so every self-test
left an item saying that the reader had approved the reader's own query, about
a screen they were already looking at. Four were sitting unread when this was
reported.
"""
from datetime import datetime, timezone

from queryhub.web import routes_queries as rq


def _dt(h=10):
    return datetime(2026, 8, 24, h, 0, tzinfo=timezone.utc)

ME = "U_ME"
SOMEONE = "U_ADMIN"


def _request(**kw):
    row = {"id": 1, "status": "completed", "decided_by_name": "An Admin",
           "decided_by_slack_id": SOMEONE, "decided_at": _dt(),
           "decision_reason": None, "row_count": 3, "database_name": "payments",
           "scheduled_for": None, "completed_at": None, "alias": "prod-main"}
    row.update(kw)
    return row


def _feed(monkeypatch, requests):
    calls = {"n": 0}

    def fake_fetch_all(sql, params=None):
        calls["n"] += 1
        return requests if "FROM requests" in sql else []

    monkeypatch.setattr(rq.db, "fetch_all", fake_fetch_all)
    return rq._derive_notifications(ME)


def test_an_admin_approving_your_query_is_worth_telling_you(monkeypatch):
    items = _feed(monkeypatch, [_request()])
    assert [i["title"] for i in items] == ["Query approved"]
    assert "An Admin approved your query on prod-main/payments" in items[0]["body"]


def test_approving_your_own_query_is_not(monkeypatch):
    # The whole complaint: the reader made this decision a second ago.
    items = _feed(monkeypatch, [_request(decided_by_slack_id=ME,
                                         decided_by_name="Me (super-admin)")])
    assert items == []


def test_an_auto_approve_grant_still_reports(monkeypatch):
    # Someone else's standing rule acted on the query. That is news.
    items = _feed(monkeypatch, [_request(decided_by_slack_id="AUTO",
                                         decided_by_name="auto-approved (grant #56)")])
    assert [i["title"] for i in items] == ["Query auto-approved"]


def test_a_rejection_is_reported_whoever_made_it(monkeypatch):
    items = _feed(monkeypatch, [_request(status="rejected", decision_reason="too broad")])
    assert [i["title"] for i in items] == ["Query rejected"]


def test_a_scheduled_run_still_reports_even_when_self_approved(monkeypatch):
    # This is the case the bell exists for: the result arrived while nobody
    # was watching. Suppressing the approval must not suppress this.
    items = _feed(monkeypatch, [_request(decided_by_slack_id=ME,
                                         scheduled_for=_dt(9),
                                         completed_at=_dt(9))])
    assert [i["title"] for i in items] == ["Scheduled query ran"]
