"""POST /api/queries carries the two super-admin flags, and refuses the fake.

The editor sends `unmasked` and `confirmed`. Both are client-supplied by
definition, so the tests that matter are the ones proving the server decides
what they mean:

  * `unmasked: true` from a plain user is 403, not a quiet downgrade.
  * a statement needing acknowledgement is 409, distinguishable from a refusal.
  * both default to false when an older client omits them entirely.

Route-level (a real TestClient through the real app), because the thing under
test is the wiring between the HTTP body and core_submit — exactly what a unit
test of either side would miss.
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from queryhub import admins, core_submit, db, requesters
from queryhub.web import app as web_app
from queryhub.web import routes_queries, sessions

USER = "U0EXAMPLE01"


@pytest.fixture
def client(monkeypatch):
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    monkeypatch.setattr(sessions, "verify_access",
                        lambda t: {"sub": USER, "sid": "sid-1",
                                   "provider": "slack"} if t == "good" else None)
    monkeypatch.setattr(sessions, "session_alive", lambda sid, principal=None: True)
    monkeypatch.setattr(admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(routes_queries.admins, "is_admin", lambda uid: True)

    target = type("T", (), {"id": 7, "alias": "svc", "enabled": True,
                            "engine": "postgres", "default_database": "app"})()
    monkeypatch.setattr(routes_queries, "_target_by_alias", lambda a: target)
    from queryhub import teams
    monkeypatch.setattr(teams, "effective_grant_for_user",
                        lambda uid, tid: {"mode": "ddl"})

    seen: dict = {}

    def fake_validate(*a, **kw):
        seen.update(kw)
        return core_submit.Rejection("query", "stopped here for the test")

    monkeypatch.setattr(core_submit, "validate_submission", fake_validate)
    monkeypatch.setattr(routes_queries.core_submit, "validate_submission",
                        fake_validate)

    with TestClient(web_app.create_app()) as c:
        c.cookies.set("qh_session", "good")
        c.seen = seen
        yield c


def _post(c, **extra):
    return c.post("/api/queries", json={"connectionId": "svc",
                                        "databaseId": "app",
                                        "sql": "SELECT 1", **extra})


def test_the_flags_reach_core_submit(client):
    _post(client, unmasked=True, confirmed=True)
    assert client.seen.get("unmasked") is True
    assert client.seen.get("confirmed") is True


def test_they_default_to_false_when_omitted(client):
    """An older client that knows nothing about either must behave as before."""
    _post(client)
    assert client.seen.get("unmasked") is False
    assert client.seen.get("confirmed") is False


def test_asking_for_unmasked_without_standing_is_403(client, monkeypatch):
    monkeypatch.setattr(
        routes_queries.core_submit, "validate_submission",
        lambda *a, **k: core_submit.Rejection(
            "unmasked", "Only a super-admin can run a query with masking "
            "turned off.", reason="not_super_admin"))
    r = _post(client, unmasked=True)
    assert r.status_code == 403, r.text
    assert "super-admin" in r.text


def test_a_statement_needing_acknowledgement_is_409(client, monkeypatch):
    monkeypatch.setattr(
        routes_queries.core_submit, "validate_submission",
        lambda *a, **k: core_submit.Rejection(
            "confirm", "DROP cannot be undone — the objects and every row in "
            "them are gone once this runs.", reason="needs_confirmation"))
    r = _post(client)
    assert r.status_code == 409, r.text
    assert "cannot be undone" in r.text, (
        "the reason text must reach the client verbatim — it is what the "
        "operator reads before answering")


def test_the_two_reasons_are_mapped_deliberately():
    """A reason with no entry falls through to 422 validation, which would be
    wrong for both of these: neither request is malformed.

    `needs_confirmation` carried `conflict` when this test was written, which
    is what it shared with `duplicate` — and that sharing turned out to be a
    live bug, not a detail: the client could only tell the two apart by
    matching message text, and the duplicate wording did not match its regex,
    so a duplicate was answered with a confirm dialog. It has its own code now,
    and this test pins that they are DIFFERENT rather than pinning either
    string in isolation."""
    assert routes_queries._REJECTION_HTTP["not_super_admin"] == (403, "forbidden")
    assert routes_queries._REJECTION_HTTP["needs_confirmation"] == (
        409, "confirmation_required")
    assert (routes_queries._REJECTION_HTTP["needs_confirmation"][1]
            != routes_queries._REJECTION_HTTP["duplicate"][1])


def test_the_body_model_declares_both_with_safe_defaults():
    fields = routes_queries.QueryIn.model_fields
    for name in ("unmasked", "confirmed"):
        assert name in fields, f"QueryIn has no {name}"
        assert fields[name].default is False, (
            f"{name} must default to False — a missing field must never mean "
            f"'yes'")
