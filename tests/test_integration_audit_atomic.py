"""A failed audit insert rolls back the change it records, on a real database.

tests/test_audit_atomic_state_changes.py proves with a fake that each route
writes its change and its audit row on one cursor. What a fake cannot show is
the database side: the triggers that fire inside the transaction (the
auth-event outbox and the access-model mirror on `requesters`) roll back with
it, and a committed run tells each person exactly once.

To make the audit insert fail, these tests name a column that does not exist,
so PostgreSQL itself rejects the statement inside the open transaction.

Every row uses ids, names and an alias that no real person or target has, and
the tests delete only those rows. The audit log is append-only, so its rows
stay behind. Each test reads only the audit rows that come after its start.
"""
import os

import pytest
from fastapi.testclient import TestClient

from queryhub import audit, auth_events, db, passwords
from queryhub.web import app as web_app
from queryhub.web import deps, login_throttle, routes_admin, routes_queries, sessions

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

SYNC = "U0ATOMSYNC"
ON, OFF = "U0ATOMIC01", "U0ATOMIC02"          # enabled, disabled at the start
PEOPLE = (ON, OFF)
EMAIL = {ON: "it-atomic-on@example.invalid", OFF: "it-atomic-off@example.invalid"}
LOCAL = "it-atomic"
LOCAL_ID = f"local:{LOCAL}"
CANCELLER = "U0ATOMIC03"
ALIAS = "it-atomic-cancel"


def _broken_audit_insert(monkeypatch):
    """The real `log_in`, with a statement that PostgreSQL refuses."""
    monkeypatch.setattr(audit, "_INSERT_SQL",
                        audit._INSERT_SQL.replace("details)", "no_such_column)"))


def _wipe():
    # No DM may come out of setting up or tearing down.
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        cur.execute("DELETE FROM requesters WHERE slack_user_id = ANY(%s)", (list(PEOPLE),))
        cur.execute("SELECT principal_id FROM principal_identity "
                    " WHERE provider = 'slack' AND external_id = ANY(%s)", (list(PEOPLE),))
        pids = [r["principal_id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM principal_identity WHERE principal_id = ANY(%s)", (pids,))
        cur.execute("DELETE FROM principal WHERE id = ANY(%s)", (pids,))
        cur.execute("DELETE FROM auth_event_outbox WHERE slack_user_id = ANY(%s)",
                    (list(PEOPLE),))
        cur.execute("DELETE FROM web_sessions WHERE slack_user_id = %s", (LOCAL_ID,))
        cur.execute("DELETE FROM local_users WHERE username = %s", (LOCAL,))
        cur.execute("DELETE FROM requests WHERE requester_slack_id = %s", (CANCELLER,))
        cur.execute("DELETE FROM target_servers WHERE alias = %s", (ALIAS,))


@pytest.fixture(autouse=True)
def since():
    """A clean slate. The value is the newest audit row id before the test."""
    login_throttle._reset_for_tests()
    _wipe()
    yield db.fetch_one("SELECT COALESCE(max(id), 0) AS n FROM audit_log")["n"]
    _wipe()
    login_throttle._reset_for_tests()


def _audit_rows(action: str, since: int, where: str, params: tuple) -> list[dict]:
    return db.fetch_all(f"SELECT details FROM audit_log WHERE action = %s AND id > %s "
                        f"AND {where} ORDER BY id", (action, since, *params))


def _client(claims):
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: claims
    return TestClient(app, raise_server_exceptions=False)


# ---- the IdP principal sync -------------------------------------------------


@pytest.fixture
def people(monkeypatch):
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        for pid, enabled in ((ON, True), (OFF, False)):
            cur.execute("INSERT INTO requesters (slack_user_id, email, name, enabled) "
                        "VALUES (%s, %s, %s, %s)",
                        (pid, EMAIL[pid], f"IT Atomic {pid[-2:]}", enabled))
    monkeypatch.setattr(routes_admin.cfg, "get_setting",
                        lambda k, d=None: SYNC if k == "idp_sync_principal" else d)
    # The real list, cut to the two test rows: a stray run against a database
    # with real people must never disable them.
    real = routes_admin.requesters.list_enabled_ids
    monkeypatch.setattr(routes_admin.requesters, "list_enabled_ids",
                        lambda: real() & set(PEOPLE))
    return _client({"sub": SYNC, "provider": "idp", "sid": None})


def _sync(client):
    # The panel now wants OFF in and ON out: one enable, one disable.
    return client.post("/api/admin/principals/sync",
                       json={"emails": [EMAIL[OFF]], "admin_emails": []})


def _enabled() -> dict:
    rows = db.fetch_all("SELECT slack_user_id, enabled FROM requesters "
                        " WHERE slack_user_id = ANY(%s)", (list(PEOPLE),))
    return {r["slack_user_id"]: r["enabled"] for r in rows}


def _mirrored() -> dict:
    rows = db.fetch_all("SELECT i.external_id, p.enabled FROM principal p "
                        "  JOIN principal_identity i ON i.principal_id = p.id "
                        " WHERE i.provider = 'slack' AND i.external_id = ANY(%s)",
                        (list(PEOPLE),))
    return {r["external_id"]: r["enabled"] for r in rows}


def _outbox() -> list[dict]:
    return db.fetch_all("SELECT table_name, op, slack_user_id, team_id, old_row, new_row "
                        "  FROM auth_event_outbox WHERE slack_user_id = ANY(%s) "
                        " ORDER BY id", (list(PEOPLE),))


def _sync_rows(since):
    return _audit_rows("idp_principal_sync", since, "actor_slack_id = %s", (SYNC,))


def test_a_failed_audit_insert_leaves_every_person_and_every_dm_as_it_was(
        people, monkeypatch, since):
    _broken_audit_insert(monkeypatch)
    r = _sync(people)
    assert r.status_code == 500
    assert _enabled() == {ON: True, OFF: False}
    assert _mirrored() == {ON: True, OFF: False}
    assert _outbox() == []                 # the trigger rows rolled back too
    assert _sync_rows(since) == []


def test_a_committed_sync_tells_each_person_once_and_writes_one_audit_row(
        people, since):
    r = _sync(people)
    assert r.status_code == 200, r.text
    assert _enabled() == {ON: False, OFF: True}
    assert _mirrored() == {ON: False, OFF: True}
    events = _outbox()
    # One row per person, on the legacy table. The mirror's own writes to
    # `principal` stay silent, although every write shares one transaction now.
    assert [(e["table_name"], e["op"], e["slack_user_id"]) for e in events] == [
        ("requesters", "UPDATE", OFF), ("requesters", "UPDATE", ON)]
    told = [auth_events.build_notifications(e) for e in events]
    assert [len(t) for t in told] == [1, 1]
    assert "re-enabled" in told[0][0][1] and "disabled" in told[1][0][1]
    [row] = _sync_rows(since)
    assert row["details"]["enabled"] == [OFF] and row["details"]["disabled"] == [ON]


# ---- the local password change ----------------------------------------------


@pytest.fixture
def local_account():
    db.execute("INSERT INTO local_users (username, password_hash, display_name) "
               "VALUES (%s, %s, 'IT Atomic')",
               (LOCAL, passwords.hash_password("oldpass12", iterations=1000)))
    sids = [sessions.create_session(LOCAL_ID, provider="local", user_agent="it")[0]
            for _ in range(2)]
    client = _client({"sub": LOCAL_ID, "provider": "local", "sid": sids[0],
                      "name": "IT Atomic"})
    return client, sids


def _change(client):
    return client.post("/api/auth/local/change-password",
                       json={"currentPassword": "oldpass12", "newPassword": "newpass34"})


def _stored_hash() -> str:
    return db.fetch_one("SELECT password_hash FROM local_users WHERE username = %s",
                        (LOCAL,))["password_hash"]


def _password_rows(since):
    return _audit_rows("local_password_changed", since, "actor_slack_id = %s",
                       (LOCAL_ID,))


def test_a_failed_audit_insert_keeps_the_old_password_and_every_session(
        local_account, monkeypatch, since):
    client, sids = local_account
    _broken_audit_insert(monkeypatch)
    assert _change(client).status_code == 500
    assert passwords.verify_password("oldpass12", _stored_hash())
    assert all(sessions.session_alive(sid) for sid in sids)
    assert _password_rows(since) == []


def test_a_committed_change_stores_the_password_ends_the_sessions_and_audits(
        local_account, since):
    client, sids = local_account
    r = _change(client)
    assert r.status_code == 200, r.text
    assert passwords.verify_password("newpass34", _stored_hash())
    assert not any(sessions.session_alive(sid) for sid in sids)
    reasons = db.fetch_all("SELECT DISTINCT revoked_reason FROM web_sessions "
                           " WHERE slack_user_id = %s", (LOCAL_ID,))
    assert reasons == [{"revoked_reason": "local password changed"}]
    [row] = _password_rows(since)
    assert row["details"] == {"sessions_revoked": 2}


# ---- the web cancel of a scheduled query ------------------------------------


@pytest.fixture
def scheduled(monkeypatch):
    target = db.insert_returning(
        "INSERT INTO target_servers (alias, host, default_database, username, "
        "  password_encrypted, enabled) "
        "VALUES (%s, '127.0.0.1', 'app', 'unused', 'unused', FALSE) RETURNING id",
        (ALIAS,))["id"]
    rid = db.insert_returning(
        "INSERT INTO requests (requester_slack_id, target_server_id, database_name, "
        "  query, status, scheduled_for) "
        "VALUES (%s, %s, 'app', 'SELECT 1', 'scheduled', NOW() + INTERVAL '1 day') "
        "RETURNING id", (CANCELLER, target))["id"]
    monkeypatch.setattr(routes_queries.deps, "require_whitelisted", lambda claims: None)
    client = _client({"sub": CANCELLER, "provider": "slack", "sid": 1,
                      "name": "Jordan Ray"})
    return client, rid


def _status(rid) -> str:
    return db.fetch_one("SELECT status::text AS s FROM requests WHERE id = %s",
                        (rid,))["s"]


def _cancel_rows(rid, since):
    return _audit_rows("cancelled", since, "request_id = %s", (rid,))


def test_a_failed_audit_insert_keeps_the_query_scheduled(scheduled, monkeypatch, since):
    client, rid = scheduled
    _broken_audit_insert(monkeypatch)
    assert client.delete(f"/api/scheduled/{rid}").status_code == 500
    assert _status(rid) == "scheduled"
    assert _cancel_rows(rid, since) == []


def test_a_committed_cancel_writes_its_audit_row(scheduled, since):
    client, rid = scheduled
    assert client.delete(f"/api/scheduled/{rid}").status_code == 204
    assert _status(rid) == "cancelled"
    [row] = _cancel_rows(rid, since)
    assert row["details"] == {"by": "requester", "phase": "scheduled"}
