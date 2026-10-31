"""The step timeline of one request: sent, decided, started, finished.

`GET /queries/{id}` serves it. The list payloads (history) do not.
"""
from __future__ import annotations

import inspect
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from queryhub.web import app as web_app
from queryhub.web import deps, mapping, routes_queries

ME = "U0EXAMPLE001"
T0 = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)
T2 = T0 + timedelta(minutes=6)
T3 = T0 + timedelta(minutes=7)


def _t(dt):
    """The display form of a step time: the format of the Messages lines."""
    return mapping.fmt_ts_ms(dt)


def _row(**kw):
    row = {"id": 41, "requester_slack_id": ME, "query": "SELECT secret FROM t",
           "status": "pending", "created_at": T0, "decided_at": None,
           "decided_by_slack_id": None, "decided_by_name": None,
           "executed_at": None, "completed_at": None, "row_count": 99,
           "required_tier": "ro"}
    row.update(kw)
    return row


def _by_sam(**kw):
    return _row(decided_at=T1, decided_by_slack_id="U0EXAMPLE009",
                decided_by_name="Sam Ellis", **kw)


def _steps(tl):
    return [s["step"] for s in tl]


def test_pending_has_only_sent():
    assert mapping.timeline(_row()) == [
        {"step": "sent", "at": T0.isoformat(), "time": _t(T0)}]


def test_approved_and_running():
    tl = mapping.timeline(_by_sam(status="executing", executed_at=T2))
    assert _steps(tl) == ["sent", "decided", "started"]
    assert tl[1] == {"step": "decided", "at": T1.isoformat(), "time": _t(T1),
                     "outcome": "approved", "by": "Sam Ellis"}


def test_completed():
    tl = mapping.timeline(_by_sam(status="completed", executed_at=T2,
                                  completed_at=T3))
    assert _steps(tl) == ["sent", "decided", "started", "finished"]
    assert tl[3] == {"step": "finished", "at": T3.isoformat(), "time": _t(T3),
                     "outcome": "completed"}


def test_failed():
    tl = mapping.timeline(_by_sam(status="failed", executed_at=T2,
                                  completed_at=T3))
    assert tl[-1]["outcome"] == "failed"


def test_denied():
    tl = mapping.timeline(_by_sam(status="rejected"))
    assert _steps(tl) == ["sent", "decided"]
    assert tl[1]["outcome"] == "denied"
    assert tl[1]["by"] == "Sam Ellis"


def test_changes_requested():
    tl = mapping.timeline(_by_sam(status="changes_requested"))
    assert tl[1]["outcome"] == "changes_requested"


def test_withdrawn_and_expired_come_from_the_audit_rows():
    w = mapping.timeline(_row(status="cancelled", decided_at=T1),
                         [{"action": "withdrawn"}])
    e = mapping.timeline(_row(status="cancelled", decided_at=T1),
                         [{"action": "request_expired"}])
    assert w[1]["outcome"] == "withdrawn"
    assert e[1]["outcome"] == "expired"
    assert "finished" not in _steps(w)


def test_cancelled_without_marker_has_no_invented_outcome():
    tl = mapping.timeline(_row(status="cancelled", decided_at=T1), [])
    assert "outcome" not in tl[1]


def test_auto_approved_at_submit_names_queryhub():
    row = _row(status="completed", decided_at=T0, decided_by_slack_id="AUTO",
               decided_by_name="auto-approve grant", executed_at=T2,
               completed_at=T3)
    tl = mapping.timeline(row)
    assert tl[1] == {"step": "decided", "at": T0.isoformat(), "time": _t(T0),
                     "outcome": "approved", "by": "QueryHub"}


def test_missing_names_leave_by_out_and_never_use_an_id():
    tl = mapping.timeline(_row(status="executing", decided_at=T1,
                               decided_by_slack_id=None, decided_by_name=None))
    assert "by" not in tl[1]
    tl = mapping.timeline(_row(status="executing", decided_at=T1,
                               decided_by_slack_id="U0EXAMPLE009"))
    assert "by" not in tl[1]


def test_name_goes_through_name_of():
    tl = mapping.timeline(_by_sam(status="executing"), None,
                          lambda n: "Jordan Ray")
    assert tl[1]["by"] == "Jordan Ray"


def test_values_are_strings_and_hold_no_query_or_count():
    tl = mapping.timeline(_by_sam(status="completed", executed_at=T2,
                                  completed_at=T3))
    for s in tl:
        assert all(isinstance(v, str) for v in s.values())
    text = repr(tl)
    assert "secret" not in text and "99" not in text


def test_every_step_carries_the_time_the_messages_show():
    row = _by_sam(status="completed", executed_at=T2, completed_at=T3)
    tl = mapping.timeline(row)
    msgs = mapping.status_messages(row)
    shown = {m["time"] for m in msgs}
    for s, dt in zip(tl, (T0, T1, T2, T3)):
        assert s["time"] == _t(dt)
        assert s["time"] in shown, "the Messages tab prints the same clock"


def test_time_follows_the_display_timezone_and_at_stays_utc(monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(mapping, "_display_tz",
                        lambda: ZoneInfo("Europe/Istanbul"))
    tl = mapping.timeline(_row())
    assert tl[0]["time"] == "2026-10-24 12:00:00.000"
    assert tl[0]["at"] == T0.isoformat()


def test_time_keeps_milliseconds():
    edge = T0.replace(microsecond=792_000)
    tl = mapping.timeline(_row(created_at=edge))
    assert tl[0]["time"].endswith(".792")


@pytest.fixture
def web(monkeypatch):
    logging.disable(logging.CRITICAL)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": ME, "provider": "slack", "sid": "s1", "name": "Jordan Ray"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: True)
    yield TestClient(app)
    logging.disable(logging.NOTSET)


def test_the_detail_route_serves_the_timeline(web, monkeypatch):
    row = _by_sam(status="completed", executed_at=T2, completed_at=T3,
                  target_server_id=7, database_name="orders", engine="postgres",
                  decision_reason=None, scheduled_for=None)
    monkeypatch.setattr(routes_queries.db, "fetch_one", lambda sql, p=None: dict(row))
    monkeypatch.setattr(routes_queries.db, "fetch_all", lambda sql, p=None: [])
    monkeypatch.setattr(routes_queries, "_alias_of", lambda tid: "prod-main")
    monkeypatch.setattr(routes_queries.people, "display_name",
                        lambda *a, **k: "Sam Ellis")
    r = web.get("/api/queries/41")
    assert r.status_code == 200, r.text
    tl = r.json()["timeline"]
    assert [s["step"] for s in tl] == ["sent", "decided", "started", "finished"]
    assert tl[1]["by"] == "Sam Ellis"


def test_the_history_list_mapping_has_no_timeline():
    assert "timeline" not in inspect.getsource(mapping.history_entry)
