"""The result endpoint sends the tier with a result that has no grid.

A statement that returns no result set has no CSV file. `GET /queries/:id/result`
answers `kind: affected` with the row count, and the client words the panel
from the count and the tier (`qhAffectedInfo` in qh-data.jsx). A read and a
write need different sentences for a count of zero, so the tier travels with it.
The old `message` stays for other clients.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from queryhub.web import app as web_app
from queryhub.web import deps, routes_queries

UID = "U0EXAMPLE001"
T0 = datetime(2026, 10, 21, 11, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def web(monkeypatch):
    logging.disable(logging.CRITICAL)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": UID, "provider": "slack", "sid": "s1", "name": "Jordan Ray"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: True)
    yield TestClient(app)
    logging.disable(logging.NOTSET)


def _row(**over):
    row = {"id": 8, "status": "completed", "requester_slack_id": UID,
           "csv_file_path": None, "row_count": 0, "query": "SELECT 1",
           "executed_at": T0, "completed_at": T0}
    row.update(over)
    return row


def _get(web, monkeypatch, row):
    monkeypatch.setattr(routes_queries.db, "fetch_one", lambda sql, p=None: dict(row))
    r = web.get("/api/queries/8/result")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_tier_the_statement_ran_at_travels_with_the_count(web, monkeypatch):
    body = _get(web, monkeypatch, _row(executed_tier="rw", required_tier="ro", row_count=0))
    assert body["kind"] == "affected" and body["affected"] == 0 and body["tier"] == "RW"


def test_the_required_tier_is_the_fallback(web, monkeypatch):
    body = _get(web, monkeypatch, _row(executed_tier=None, required_tier="ddl"))
    assert body["tier"] == "DDL"


def test_a_row_with_neither_is_read_only(web, monkeypatch):
    body = _get(web, monkeypatch, _row(executed_tier=None, required_tier=None))
    assert body["tier"] == "RO"


def test_the_count_and_the_old_message_are_kept(web, monkeypatch):
    body = _get(web, monkeypatch, _row(executed_tier="rw", row_count=5))
    assert body["affected"] == 5 and body["message"] == "5 row(s) affected."
    unknown = _get(web, monkeypatch, _row(executed_tier="rw", row_count=None))
    assert unknown["affected"] is None and unknown["message"] == "? row(s) affected."
