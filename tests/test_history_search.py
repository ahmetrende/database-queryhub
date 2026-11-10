"""Search, date range and duration on GET /history.

- No search parameter gives the old WHERE clause and the old parameters.
- `q` matches the SQL text or the database name with ILIKE, and the request id
  when it is digits with an optional '#'. LIKE wildcards in `q` are literal.
- `since` and `until` are whole days on the caller's clock (`tz`, minutes east
  of UTC, default 0). `until` includes its whole day.
- Each row carries `durationMs`: completed_at minus executed_at.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from queryhub.web import app as web_app
from queryhub.web import deps, mapping, routes_data

USER = "U0EXAMPLE001"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
OLD_WHERE = ("FROM requests WHERE requester_slack_id = %s AND status <> 'draft' "
             "ORDER BY id DESC LIMIT %s")


@pytest.fixture
def web(monkeypatch):
    logging.disable(logging.CRITICAL)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": USER, "provider": "slack", "sid": "s1", "name": "Ex"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(routes_data.admins, "is_super_admin", lambda uid: True)
    yield TestClient(app)
    logging.disable(logging.NOTSET)


def _hrow(rid, **kw):
    row = {"id": rid, "query": "SELECT * FROM orders", "target_server_id": 7,
           "database_name": "shop", "status": "completed", "row_count": 1,
           "created_at": NOW, "decided_by_slack_id": USER,
           "decided_by_name": "Ex", "executed_at": NOW, "completed_at": None,
           "executed_target_id": None, "executed_tier": "ro", "engine": "postgres"}
    row.update(kw)
    return row


def _env(monkeypatch, rows):
    seen = []

    def fetch_all(sql, p=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT id, query, target_server_id"):
            seen.append((flat, p))
            return [dict(r) for r in rows]
        if "FROM audit_log" in flat:
            return []
        raise AssertionError(f"unexpected read: {flat}")
    monkeypatch.setattr(routes_data.db, "fetch_all", fetch_all)
    monkeypatch.setattr(routes_data, "_alias_of",
                        lambda tid: {7: "srv-alpha"}.get(tid))
    monkeypatch.setattr(routes_data, "_conn_state_resolver", lambda uid: lambda tid: "ok")
    monkeypatch.setattr(routes_data.people, "namer", lambda names: lambda n: n)
    return seen


def test_no_parameters_keep_the_old_query(web, monkeypatch):
    seen = _env(monkeypatch, [_hrow(1)])
    r = web.get("/api/history")
    assert r.status_code == 200, r.text
    sql, params = seen[0]
    assert sql.endswith(OLD_WHERE)
    assert "ILIKE" not in sql and "created_at >=" not in sql
    assert params == (USER, 50)


def test_blank_search_is_no_search(web, monkeypatch):
    seen = _env(monkeypatch, [])
    web.get("/api/history", params={"q": "   "})
    sql, params = seen[0]
    assert sql.endswith(OLD_WHERE) and params == (USER, 50)


def test_wildcards_in_the_search_are_literal(web, monkeypatch):
    seen = _env(monkeypatch, [])
    r = web.get("/api/history", params={"q": " 50%_off "})
    assert r.status_code == 200, r.text
    sql, params = seen[0]
    assert "(query ILIKE %s ESCAPE '\\' OR database_name ILIKE %s ESCAPE '\\')" in sql
    assert " OR id = %s" not in sql
    assert params == (USER, "%50\\%\\_off%", "%50\\%\\_off%", 50)


def test_a_backslash_in_the_search_is_literal(web, monkeypatch):
    seen = _env(monkeypatch, [])
    web.get("/api/history", params={"q": "a\\b"})
    assert seen[0][1][1] == "%a\\\\b%"


def test_a_hash_number_also_matches_the_request_id(web, monkeypatch):
    seen = _env(monkeypatch, [])
    web.get("/api/history", params={"q": "#123"})
    sql, params = seen[0]
    assert " OR id = %s)" in sql
    assert params == (USER, "%#123%", "%#123%", 123, 50)


def test_a_plain_number_also_matches_the_request_id(web, monkeypatch):
    seen = _env(monkeypatch, [])
    web.get("/api/history", params={"q": "123"})
    assert seen[0][1] == (USER, "%123%", "%123%", 123, 50)


def test_the_date_range_covers_whole_days(web, monkeypatch):
    seen = _env(monkeypatch, [])
    r = web.get("/api/history", params={"since": "2026-09-01", "until": "2026-09-03"})
    assert r.status_code == 200, r.text
    sql, params = seen[0]
    assert "AND created_at >= %s AND created_at < %s ORDER BY" in sql
    assert params == (USER, datetime(2026, 9, 1, tzinfo=timezone.utc),
                      datetime(2026, 9, 4, tzinfo=timezone.utc), 50)


def test_one_day_range(web, monkeypatch):
    seen = _env(monkeypatch, [])
    web.get("/api/history", params={"since": "2026-09-01", "until": "2026-09-01"})
    assert seen[0][1][1:3] == (datetime(2026, 9, 1, tzinfo=timezone.utc),
                               datetime(2026, 9, 2, tzinfo=timezone.utc))


def test_the_days_follow_the_callers_clock(web, monkeypatch):
    seen = _env(monkeypatch, [])
    r = web.get("/api/history", params={"since": "2026-09-01", "until": "2026-09-01", "tz": 180})
    assert r.status_code == 200, r.text
    since, until = seen[0][1][1:3]
    # Midnight in UTC+3 is 21:00 UTC on the day before.
    assert since == datetime(2026, 8, 31, 21, 0, tzinfo=timezone.utc)
    assert until == datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc)


def test_an_offset_out_of_range_is_refused(web, monkeypatch):
    _env(monkeypatch, [])
    assert web.get("/api/history", params={"since": "2026-09-01", "tz": 900}).status_code == 422


def test_search_and_dates_together(web, monkeypatch):
    seen = _env(monkeypatch, [])
    web.get("/api/history", params={"q": "orders", "until": "2026-09-03", "limit": 200})
    assert seen[0][1] == (USER, "%orders%", "%orders%",
                          datetime(2026, 9, 4, tzinfo=timezone.utc), 200)


def test_an_end_before_the_start_is_refused(web, monkeypatch):
    seen = _env(monkeypatch, [])
    r = web.get("/api/history", params={"since": "2026-09-03", "until": "2026-09-01"})
    assert r.status_code == 400
    assert "bad_request" in r.text
    assert seen == []


def test_a_search_over_200_characters_is_refused(web, monkeypatch):
    seen = _env(monkeypatch, [])
    assert web.get("/api/history", params={"q": "x" * 201}).status_code == 422
    assert web.get("/api/history", params={"q": "x" * 200}).status_code == 200
    assert len(seen) == 1


def test_a_bad_date_is_refused(web, monkeypatch):
    _env(monkeypatch, [])
    assert web.get("/api/history", params={"since": "yesterday"}).status_code == 422


def test_rows_carry_the_run_duration(web, monkeypatch):
    rows = [_hrow(3, completed_at=NOW + timedelta(seconds=1.5)),
            _hrow(2, completed_at=None),
            _hrow(1, completed_at=NOW - timedelta(seconds=2)),
            _hrow(0, status="pending", executed_at=None)]
    _env(monkeypatch, rows)
    got = {h["id"]: h["durationMs"] for h in web.get("/api/history").json()["history"]}
    assert got == {"3": 1500, "2": None, "1": 0, "0": None}


def test_the_mapper_works_without_the_timing_columns():
    row = _hrow(5)
    row.pop("executed_at")
    row.pop("completed_at")
    assert mapping.history_entry(row, lambda tid: "srv-alpha")["durationMs"] is None
    assert mapping.run_duration_ms(NOW, NOW + timedelta(milliseconds=850)) == 850
    assert mapping.run_duration_ms(None, NOW) is None
