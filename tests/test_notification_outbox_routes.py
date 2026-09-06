"""GET/POST /api/admin/notifications/outbox — task A2 of PLA-479 Phase 1d.

The IDP panel has no admins table and must not grow one, so QueryHub decides
recipients (migrations/102_notification_outbox.sql, written by
core_submit.dispatch_and_notify — see tests/test_notification_outbox.py for
that half) and the panel polls this pair of routes: GET lists what's
unprocessed, POST stamps one row done.

Most assertions here call the handler functions directly rather than going
through TestClient/HTTP: the property under test is what SQL the handler
issues and how it reshapes the rows, not the ASGI plumbing (that plumbing is
already covered fleet-wide by test_admin_routes_gated.py's auto-discovery).
A couple of end-to-end TestClient tests at the bottom confirm the HTTP
wiring — status codes, an empty 204 body, a 403 for a real non-admin session.

FakeOutboxDB is not a general SQL engine. It knows the exact shape of this
route's own queries and ASSERTS on that shape (the WHERE/ORDER BY/LIMIT
clauses) before simulating what Postgres would hand back — so a test here
fails for the right reason if the SQL regresses, not only if the Python
reshaping does.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from queryhub import admins, db, requesters
from queryhub.web import app as web_app
from queryhub.web import routes_admin, sessions

ADMIN = "U0ADMIN"


class FakeOutboxDB:
    """Stands in for notification_outbox + admins + requesters."""

    def __init__(self, outbox_rows, admin_emails=None, requester_emails=None):
        self.rows = {r["id"]: dict(r) for r in outbox_rows}
        self.admin_emails = admin_emails or {}
        self.requester_emails = requester_emails or {}

    def fetch_all(self, sql, params=None):
        if "FROM notification_outbox" in sql:
            assert "processed_at IS NULL" in sql, (
                f"GET must filter to unprocessed rows only: {sql!r}")
            assert "ORDER BY created_at, id" in sql, (
                f"GET must order by (created_at, id) to match the pending "
                f"index and break created_at ties deterministically: {sql!r}")
            assert "LIMIT %s" in sql, f"GET must respect a caller limit: {sql!r}"
            pending = [dict(r) for r in self.rows.values()
                      if r["processed_at"] is None]
            pending.sort(key=lambda r: (r["created_at"], r["id"]))
            limit = params[-1] if params else len(pending)
            return pending[:limit]
        if "FROM admins" in sql:
            ids = params[0]
            return [{"slack_user_id": sid, "email": self.admin_emails[sid]}
                    for sid in ids if sid in self.admin_emails]
        if "FROM requesters" in sql:
            ids = params[0]
            return [{"slack_user_id": sid, "email": self.requester_emails[sid]}
                    for sid in ids if sid in self.requester_emails]
        raise AssertionError(f"unexpected fetch_all: {sql!r}")

    def fetch_one(self, sql, params=None):
        """Only used by a deliberately-broken variant of the POST handler
        during guard-break testing (see task-A2-report.md) — the shipped
        handler never calls fetch_one at all, by design."""
        if "FROM notification_outbox" in sql:
            row = self.rows.get(params[0])
            return row if row is not None and row["processed_at"] is None else None
        raise AssertionError(f"unexpected fetch_one: {sql!r}")

    def execute(self, sql, params=None):
        if "UPDATE notification_outbox" in sql:
            assert "processed_at IS NULL" in sql, (
                f"POST /processed must guard its UPDATE with "
                f"processed_at IS NULL, or a second call would re-fire "
                f"whatever a stamp is supposed to be idempotent against: "
                f"{sql!r}")
            outbox_id = params[0]
            row = self.rows.get(outbox_id)
            if row is not None and row["processed_at"] is None:
                row["processed_at"] = datetime.now(timezone.utc)
            return
        raise AssertionError(f"unexpected execute: {sql!r}")


def _row(id_, *, created_at, processed_at=None, recipients=(),
        request_id=1, event_type="queryhub.request_pending", payload=None):
    return {
        "id": id_, "event_type": event_type, "request_id": request_id,
        "recipients": list(recipients),
        "payload": payload or {"requesterName": "Ada", "tier": "RO",
                               "target": "prod-primary", "justification": "j",
                               "requestId": request_id},
        "created_at": created_at, "processed_at": processed_at,
    }


@pytest.fixture
def admin_claims():
    return {"sub": ADMIN, "name": "Admin Person"}


@pytest.fixture(autouse=True)
def _admin_gate(monkeypatch):
    monkeypatch.setattr(admins, "is_admin", lambda uid: uid == ADMIN)
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: False)


# ---------------------------------------------------------------------------
# GET /notifications/outbox — direct handler calls
# ---------------------------------------------------------------------------

def test_lists_unprocessed_only_oldest_first_and_respects_the_limit(
        monkeypatch, admin_claims):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 3, tzinfo=timezone.utc)
    fake = FakeOutboxDB([
        _row(3, created_at=t1),   # ties row 1 on created_at
        _row(1, created_at=t1),
        _row(2, created_at=t2),   # would sort last of the pending rows
        _row(4, created_at=t3, processed_at=t3),  # already processed
    ])
    monkeypatch.setattr(db, "fetch_all", fake.fetch_all)

    result = routes_admin.notifications_outbox(limit=2, claims=admin_claims)

    ids = [e["id"] for e in result["events"]]
    # Row 4 (processed) never appears. Rows 1 and 3 tie on created_at, so the
    # trailing `id` in ORDER BY decides — 1 before 3. limit=2 then cuts off
    # row 2 even though it is unprocessed and older than nothing skipped.
    assert ids == [1, 3], (
        "expected unprocessed-only + (created_at, id) ordering + limit=2, "
        f"got {ids}")


def test_a_higher_limit_reaches_the_row_the_first_test_cut_off(
        monkeypatch, admin_claims):
    """Companion to the test above: proves limit=2's exclusion of row 2 was
    the limit acting, not row 2 being silently unreachable some other way."""
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    fake = FakeOutboxDB([_row(1, created_at=t1), _row(2, created_at=t2)])
    monkeypatch.setattr(db, "fetch_all", fake.fetch_all)

    result = routes_admin.notifications_outbox(limit=10, claims=admin_claims)

    assert [e["id"] for e in result["events"]] == [1, 2]


def test_recipients_resolve_to_work_emails_and_unresolved_are_counted(
        monkeypatch, admin_claims):
    """Ruling P-1: stored recipients are slack_user_id; served recipients
    are work emails resolved at READ time, and a recipient with no
    resolvable email is omitted from the list and counted, not dropped
    silently."""
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake = FakeOutboxDB(
        [_row(1, created_at=t1, recipients=["U0A", "U0B", "U0GONE"])],
        admin_emails={"U0A": "a@example.com"},
        requester_emails={"U0B": "b@example.com"},
    )
    monkeypatch.setattr(db, "fetch_all", fake.fetch_all)

    result = routes_admin.notifications_outbox(limit=50, claims=admin_claims)

    assert result["events"][0]["recipients"] == ["a@example.com", "b@example.com"]
    assert result["unresolvedRecipients"] == 1


def test_event_shape_is_camelcase_and_passes_the_payload_through(
        monkeypatch, admin_claims):
    """Ruling P-9 (pinned wire shape): camelCase keys throughout even though
    the columns are snake_case. Payload values are already strings here
    (Ruling P-10's coercion is exercised separately, below, against a row
    that carries a non-string value the way A1's writer actually stores
    one)."""
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake = FakeOutboxDB([_row(
        12, created_at=t1, recipients=["U0A"], request_id=1042,
        payload={"requesterName": "Ada", "tier": "RW", "target": "prod",
                "justification": "need it", "requestId": "1042"})],
        admin_emails={"U0A": "a@example.com"})
    monkeypatch.setattr(db, "fetch_all", fake.fetch_all)

    result = routes_admin.notifications_outbox(limit=50, claims=admin_claims)

    event = result["events"][0]
    assert event["id"] == 12
    assert event["eventType"] == "queryhub.request_pending"
    assert event["requestId"] == 1042
    assert event["createdAt"] == t1.isoformat()
    assert event["payload"] == {"requesterName": "Ada", "tier": "RW",
                                "target": "prod", "justification": "need it",
                                "requestId": "1042"}
    assert set(event.keys()) == {"id", "eventType", "requestId", "recipients",
                                 "payload", "createdAt"}


def test_payload_values_are_always_served_as_strings(monkeypatch, admin_claims):
    """Ruling P-10 (cross-repo defect found wiring the panel side): the
    panel decodes `Payload map[string]string`, and Go's json.Unmarshal
    refuses a JSON NUMBER into a string field for the WHOLE response — not
    just that one key — so a single non-string payload value anywhere in
    the batch would silently stop every pending notification from being
    delivered. A1's own writer stores `"requestId": row["id"]`, a Python
    int (core_submit.py), which is exactly this shape. Rather than trust
    every present and future writer to remember the panel's map[string]string
    contract, this endpoint coerces at the read boundary — the one place
    that can't regress silently again.

    Deliberately exercises a bool and a None too, not just the one int A1
    happens to produce today: the guarantee is "every payload value is a
    string", not "the one field we've seen break so far".
    """
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake = FakeOutboxDB([_row(
        1, created_at=t1,
        payload={"requestId": 1042, "escalated": True, "note": None,
                "target": "prod-primary"})])
    monkeypatch.setattr(db, "fetch_all", fake.fetch_all)

    result = routes_admin.notifications_outbox(limit=50, claims=admin_claims)

    payload = result["events"][0]["payload"]
    assert all(isinstance(v, str) for v in payload.values()), (
        f"every payload value must be a string (Ruling P-10): {payload!r}")
    assert payload["requestId"] == "1042"
    assert payload["target"] == "prod-primary"


def test_a_null_request_id_is_served_as_null(monkeypatch, admin_claims):
    """migration 102: request_id is nullable (an event with no linked
    request)."""
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake = FakeOutboxDB([_row(1, created_at=t1, request_id=None)])
    monkeypatch.setattr(db, "fetch_all", fake.fetch_all)

    result = routes_admin.notifications_outbox(limit=50, claims=admin_claims)

    assert result["events"][0]["requestId"] is None


def test_get_requires_admin(admin_claims):
    with pytest.raises(Exception) as e:
        routes_admin.notifications_outbox(limit=50, claims={"sub": "U0PLAIN"})
    assert e.value.status_code == 403


# ---------------------------------------------------------------------------
# POST /notifications/outbox/{id}/processed — direct handler calls
# ---------------------------------------------------------------------------

def test_marking_processed_stamps_the_row(monkeypatch, admin_claims):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake = FakeOutboxDB([_row(5, created_at=t1)])
    monkeypatch.setattr(db, "execute", fake.execute)
    monkeypatch.setattr(db, "fetch_one", fake.fetch_one)

    routes_admin.notifications_outbox_processed(5, claims=admin_claims)

    assert fake.rows[5]["processed_at"] is not None


def test_a_second_post_for_an_already_processed_row_is_still_204(
        monkeypatch, admin_claims):
    """The contract detail that matters: the panel's poller is at-least-once
    by construction (it can crash between delivering and stamping), so a
    re-delivery must be cheap and a re-stamp must be silent — a 204, not an
    error."""
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake = FakeOutboxDB([_row(5, created_at=t1)])
    monkeypatch.setattr(db, "execute", fake.execute)
    monkeypatch.setattr(db, "fetch_one", fake.fetch_one)

    routes_admin.notifications_outbox_processed(5, claims=admin_claims)
    stamped_at = fake.rows[5]["processed_at"]
    # Must not raise, and must not disturb the timestamp already recorded.
    routes_admin.notifications_outbox_processed(5, claims=admin_claims)

    assert fake.rows[5]["processed_at"] == stamped_at


def test_posting_processed_for_an_id_that_never_existed_is_also_204(
        monkeypatch, admin_claims):
    """The atomic UPDATE-with-guard design doesn't distinguish "already
    processed" from "never existed" — both update zero rows. Must not raise
    either way."""
    fake = FakeOutboxDB([])
    monkeypatch.setattr(db, "execute", fake.execute)
    monkeypatch.setattr(db, "fetch_one", fake.fetch_one)

    routes_admin.notifications_outbox_processed(999, claims=admin_claims)


def test_post_requires_admin():
    with pytest.raises(Exception) as e:
        routes_admin.notifications_outbox_processed(
            5, claims={"sub": "U0PLAIN"})
    assert e.value.status_code == 403


# ---------------------------------------------------------------------------
# End-to-end HTTP wiring: status codes, an empty 204 body, a real 403.
# ---------------------------------------------------------------------------

@pytest.fixture
def http_client(monkeypatch):
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    monkeypatch.setattr(sessions, "verify_access",
                        lambda t: {"sub": ADMIN, "sid": "sid-1",
                                   "provider": "slack"} if t == "good" else
                                  {"sub": "U0PLAIN", "sid": "sid-2",
                                   "provider": "slack"} if t == "plain"
                                  else None)
    monkeypatch.setattr(sessions, "session_alive", lambda sid, principal=None: True)
    monkeypatch.setattr(requesters, "is_allowed", lambda uid: True)
    from queryhub.web import admin as web_admin
    monkeypatch.setattr(web_admin.admins, "is_admin", lambda uid: uid == ADMIN)
    monkeypatch.setattr(web_admin.admins, "is_super_admin", lambda uid: False)

    with TestClient(web_app.create_app()) as c:
        yield c


def test_http_get_returns_the_pinned_shape(monkeypatch, http_client):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake = FakeOutboxDB([_row(1, created_at=t1, recipients=["U0A"])],
                        admin_emails={"U0A": "a@example.com"})
    monkeypatch.setattr(routes_admin.db, "fetch_all", fake.fetch_all)
    http_client.cookies.set("qh_session", "good")

    r = http_client.get("/api/admin/notifications/outbox?limit=50")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["events"][0]["id"] == 1
    assert body["unresolvedRecipients"] == 0


def test_http_post_processed_is_204_with_an_empty_body(monkeypatch, http_client):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake = FakeOutboxDB([_row(7, created_at=t1)])
    monkeypatch.setattr(routes_admin.db, "execute", fake.execute)
    monkeypatch.setattr(routes_admin.db, "fetch_one", fake.fetch_one)
    http_client.cookies.set("qh_session", "good")

    r = http_client.post("/api/admin/notifications/outbox/7/processed")

    assert r.status_code == 204
    assert r.content == b""


def test_http_non_admin_gets_403_from_both_routes(http_client):
    http_client.cookies.set("qh_session", "plain")

    r1 = http_client.get("/api/admin/notifications/outbox?limit=50")
    r2 = http_client.post("/api/admin/notifications/outbox/1/processed")

    assert r1.status_code == 403, r1.text
    assert r2.status_code == 403, r2.text
    assert r1.json()["error"]["code"] == "forbidden"
    assert r2.json()["error"]["code"] == "forbidden"
