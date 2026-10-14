"""A state change and its audit row commit in one transaction, or not at all.

Three routes wrote the change and the audit row as separate commits:

- the local password change (password, then sessions, then the audit row),
- the IdP principal sync (one commit per person, then the audit row),
- the web cancel of a scheduled query (the cancel, then the audit row).

A failure between two commits left a change that nobody could trace. Now each
route runs its writes and the audit insert on ONE cursor of ONE
`db.transaction()`. The fake database below keeps the only rule that matters
here: a transaction commits all of its statements, or none of them. A real
rollback, with the auth-event triggers, runs in
tests/test_integration_audit_atomic.py.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient

from queryhub import db, local_users, passwords, requesters
from queryhub.idp_sync_guard import Verdict
from queryhub.web import app as web_app
from queryhub.web import deps, login_throttle, routes_admin, routes_queries, sessions

SYNC = "U0SYNC0001"
GONE = "U0GONE0001"
NEW = "U0NEW00001"


class FakeCursor:
    def __init__(self, fake: "FakeDB"):
        self.fake = fake
        self.statements: list[tuple[str, tuple | None]] = []
        self.rowcount = 0
        self._row: dict | None = None

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        if self.fake.fail_on and self.fake.fail_on in flat:
            raise psycopg.OperationalError(f"the statement failed: {self.fake.fail_on}")
        self.statements.append((flat, params))
        self._row = next((row for part, row in self.fake.rows.items() if part in flat),
                         None)
        self.rowcount = next((n for part, n in self.fake.rowcounts.items() if part in flat),
                             1)

    def fetchone(self):
        return self._row


class FakeDB:
    """Commits a transaction whole when its block ends, and rolls it back
    whole when its block raises. `fail_on` makes the first statement that
    contains this text raise."""

    def __init__(self):
        self.committed: list[list[tuple[str, tuple | None]]] = []
        self.rolled_back: list[list[tuple[str, tuple | None]]] = []
        self.rows: dict[str, dict] = {}
        self.rowcounts: dict[str, int] = {}
        self.fail_on: str | None = None
        self.opened = 0

    @contextmanager
    def transaction(self):
        self.opened += 1
        cur = FakeCursor(self)
        try:
            yield cur
        except BaseException:
            self.rolled_back.append(cur.statements)
            raise
        self.committed.append(cur.statements)


def _what(sql: str) -> str:
    """`UPDATE requesters`, `INSERT audit_log`, `SELECT local_users`."""
    if sql.startswith("SELECT"):
        return "SELECT " + re.search(r"FROM (\w+)", sql).group(1)
    verb, table = re.match(r"(UPDATE|INSERT INTO) (\w+)", sql).groups()
    return f"{verb.split()[0]} {table}"


def _shape(txn) -> list[str]:
    return [_what(sql) for sql, _ in txn]


def _audit_params(txn) -> tuple:
    [params] = [p for sql, p in txn if sql.startswith("INSERT INTO audit_log")]
    return params


@pytest.fixture
def fake_db(monkeypatch):
    """Every module reaches the same `db` module, so one patch covers them all.
    A standalone `db.execute` still meets the conftest barrier and fails."""
    fake = FakeDB()
    monkeypatch.setattr(db, "transaction", fake.transaction)
    return fake


# ---- the IdP principal sync -------------------------------------------------


@pytest.fixture
def sync_client(monkeypatch, fake_db):
    monkeypatch.setattr(routes_admin.cfg, "get_setting",
                        lambda k, d=None: SYNC if k == "idp_sync_principal" else d)
    monkeypatch.setattr(routes_admin.access_model, "role_holder_ids", lambda: set())
    monkeypatch.setattr(routes_admin.admins, "list_active", lambda: [])
    monkeypatch.setattr(routes_admin.requesters, "list_enabled_ids", lambda: {GONE})
    monkeypatch.setattr(routes_admin.requesters, "principal_by_email",
                        lambda e: {"slack_user_id": NEW} if e == "new@example.com" else None)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = \
        lambda: {"sub": SYNC, "provider": "idp", "sid": None}
    return TestClient(app, raise_server_exceptions=False)


def _sync(client):
    return client.post("/api/admin/principals/sync",
                       json={"emails": ["new@example.com"], "admin_emails": []})


def test_the_sync_commits_every_change_and_the_audit_row_together(sync_client, fake_db):
    r = _sync(sync_client)
    assert r.status_code == 200, r.text
    assert fake_db.rolled_back == []
    [txn] = fake_db.committed
    assert _shape(txn) == ["UPDATE requesters", "UPDATE requesters", "INSERT audit_log"]
    assert [p for _, p in txn[:2]] == [(NEW,), (GONE,)]
    assert "enabled = TRUE" in txn[0][0] and "enabled = FALSE" in txn[1][0]
    params = _audit_params(txn)
    assert params[1] == SYNC and params[3] == "idp_principal_sync"
    details = json.loads(params[4])
    assert details["enabled"] == [NEW] and details["disabled"] == [GONE]


def test_a_failing_audit_insert_rolls_back_every_change_of_the_sync(sync_client, fake_db):
    """The point of the change. Before it, both people were already changed
    when the audit insert failed, and no row said who did it."""
    fake_db.fail_on = "INSERT INTO audit_log"
    r = _sync(sync_client)
    assert r.status_code == 500
    assert fake_db.committed == []
    [txn] = fake_db.rolled_back
    assert _shape(txn) == ["UPDATE requesters", "UPDATE requesters"]


@pytest.mark.parametrize("state, status, transactions", [("pending", 409, 0),
                                                         ("approved", 200, 1)])
def test_the_guard_decides_before_the_transaction_opens(
        sync_client, fake_db, monkeypatch, state, status, transactions):
    """The gate spends an approval in its own commit, so it must run before
    any write. A held run opens no transaction at all."""
    open_at_gate = []

    def gate(ids, *, actor=None):
        open_at_gate.append(fake_db.opened)
        return Verdict(state, 0, len(ids), 3)
    monkeypatch.setattr(routes_admin.idp_sync_guard, "gate", gate)
    assert _sync(sync_client).status_code == status
    assert open_at_gate == [0]
    assert fake_db.opened == transactions


# ---- the local password change ----------------------------------------------

USER = "local:alice"
TKEY = "pwchange:alice"


@pytest.fixture
def password_client(monkeypatch, fake_db):
    login_throttle._reset_for_tests()
    # Few iterations: the test reads the old hash fast. The new hash still
    # gets the production work factor.
    fake_db.rows["FROM local_users"] = {
        "username": "alice", "enabled": True, "must_change_pw": False,
        "password_hash": passwords.hash_password("oldpass12", iterations=1000)}
    fake_db.rowcounts["UPDATE web_sessions"] = 2
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = \
        lambda: {"sub": USER, "provider": "local", "sid": 7, "name": "Alice"}
    yield TestClient(app, raise_server_exceptions=False)
    login_throttle._reset_for_tests()


def _change(client, current="oldpass12", new="newpass34"):
    return client.post("/api/auth/local/change-password",
                       json={"currentPassword": current, "newPassword": new})


def _failures() -> int:
    return len(login_throttle._FAILURES.get(TKEY, ()))


def test_the_password_sessions_and_audit_row_commit_together(password_client, fake_db):
    login_throttle.record_failure(TKEY)
    r = _change(password_client)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "reauth": True}
    [txn] = fake_db.committed
    assert _shape(txn) == ["SELECT local_users", "UPDATE local_users",
                           "UPDATE web_sessions", "INSERT audit_log"]
    new_hash, must_change, name = txn[1][1]
    assert passwords.verify_password("newpass34", new_hash)
    assert (must_change, name) == (False, "alice")
    assert txn[2][1] == ("local password changed", USER)
    params = _audit_params(txn)
    assert params[1:4] == (USER, "Alice", "local_password_changed")
    assert json.loads(params[4]) == {"sessions_revoked": 2}
    # The browser drops both cookies, and the throttle forgets the old guess.
    cookies = " ".join(r.headers.get_list("set-cookie"))
    assert f"{deps.SESSION_COOKIE}=" in cookies and f"{deps.REFRESH_COOKIE}=" in cookies
    assert "Max-Age=0" in cookies
    assert _failures() == 0


def test_a_failing_audit_insert_keeps_the_old_password_and_the_sessions(
        password_client, fake_db):
    """Before the change, this left a new password, revoked sessions and no
    audit row. Now nothing commits, and the user sees an error."""
    login_throttle.record_failure(TKEY)
    fake_db.fail_on = "INSERT INTO audit_log"
    r = _change(password_client)
    assert r.status_code == 500
    assert fake_db.committed == []
    [txn] = fake_db.rolled_back
    assert _shape(txn) == ["SELECT local_users", "UPDATE local_users",
                           "UPDATE web_sessions"]
    assert _failures() == 1          # neither cleared nor counted as a guess


def test_a_wrong_current_password_writes_nothing_and_counts_a_guess(
        password_client, fake_db):
    r = _change(password_client, current="WRONGpass1")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "bad_credentials"
    assert [_shape(t) for t in fake_db.committed] == [["SELECT local_users"]]
    assert _failures() == 1


@pytest.mark.parametrize("new, code", [("short", "weak_password"),
                                       ("oldpass12", "same_password")])
def test_a_refused_new_password_reads_and_writes_nothing(
        password_client, fake_db, new, code):
    r = _change(password_client, new=new)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == code
    assert fake_db.committed == [[]]
    assert _failures() == 0


def test_a_throttled_change_opens_no_transaction(password_client, fake_db, monkeypatch):
    monkeypatch.setattr(login_throttle, "_max_failures", lambda: 1)
    login_throttle.record_failure(TKEY)
    r = _change(password_client)
    assert r.status_code == 429
    assert fake_db.opened == 0


# ---- the web cancel of a scheduled query ------------------------------------

REQUESTER = "U0AB12CD34"


@pytest.fixture
def cancel_client(monkeypatch, fake_db):
    monkeypatch.setattr(routes_queries.deps, "require_whitelisted", lambda claims: None)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = \
        lambda: {"sub": REQUESTER, "provider": "slack", "sid": 1, "name": "Jordan Ray"}
    return TestClient(app, raise_server_exceptions=False)


def test_the_cancel_and_its_audit_row_commit_together(cancel_client, fake_db):
    fake_db.rows["UPDATE requests"] = {"id": 41}
    r = cancel_client.delete("/api/scheduled/41")
    assert r.status_code == 204, r.text
    [txn] = fake_db.committed
    assert _shape(txn) == ["UPDATE requests", "INSERT audit_log"]
    assert txn[0][1] == (41, REQUESTER)
    params = _audit_params(txn)
    assert params[:4] == (41, REQUESTER, "Jordan Ray", "cancelled")
    assert json.loads(params[4]) == {"by": "requester", "phase": "scheduled"}


def test_a_failing_audit_insert_keeps_the_query_scheduled(cancel_client, fake_db):
    fake_db.rows["UPDATE requests"] = {"id": 41}
    fake_db.fail_on = "INSERT INTO audit_log"
    r = cancel_client.delete("/api/scheduled/41")
    assert r.status_code == 500
    assert fake_db.committed == []
    assert [_shape(t) for t in fake_db.rolled_back] == [["UPDATE requests"]]


def test_cancelling_a_query_that_is_not_scheduled_writes_no_audit_row(
        cancel_client, fake_db):
    r = cancel_client.delete("/api/scheduled/41")
    assert r.status_code == 404
    assert [_shape(t) for t in fake_db.committed] == [["UPDATE requests"]]


# ---- the cursor variants change nothing for the old callers ------------------


class _Recorder:
    def __init__(self):
        self.statements = []
        self.rowcount = 3

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))


def test_enable_and_disable_run_the_same_statement_on_either_path(monkeypatch):
    standalone = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None: standalone.append(
        (" ".join(sql.split()), params)))
    requesters.enable(NEW)
    requesters.disable(GONE)
    cur = _Recorder()
    requesters.enable_in(cur, NEW)
    requesters.disable_in(cur, GONE)
    assert cur.statements == standalone


def test_revoke_user_runs_the_cursor_variant_in_its_own_transaction(fake_db):
    fake_db.rowcounts["UPDATE web_sessions"] = 4
    assert sessions.revoke_user(GONE, "offboarded") == 4
    [txn] = fake_db.committed
    assert txn == [(txn[0][0], ("offboarded", GONE))]
    assert "WHERE slack_user_id = %s AND revoked_at IS NULL" in txn[0][0]
    cur = _Recorder()
    assert sessions.revoke_user_in(cur, GONE, "offboarded") == 3
    assert cur.statements == txn


@pytest.mark.parametrize("row, current, new, code", [
    ("alice", "oldpass12", "short", "weak_password"),
    ("alice", "oldpass12", "oldpass12", "same_password"),
    (None, "oldpass12", "newpass34", "bad_credentials"),
    ("disabled", "oldpass12", "newpass34", "bad_credentials"),
    ("alice", "WRONGpass1", "newpass34", "bad_credentials"),
    ("alice", "oldpass12", "newpass34", ""),
])
def test_both_password_paths_give_the_same_answer(monkeypatch, row, current, new, code):
    """One set of rules behind both entry points: same codes, same order."""
    stored = {"alice": {"enabled": True}, "disabled": {"enabled": False}}.get(row)
    if stored is not None:
        stored["password_hash"] = passwords.hash_password("oldpass12", iterations=1000)
    writes = []
    monkeypatch.setattr(local_users, "get", lambda name: stored)
    monkeypatch.setattr(local_users, "set_password",
                        lambda name, h, must_change_pw=False: writes.append((name, h)))
    assert local_users.change_password("alice", current, new) == code

    class _Cur(_Recorder):
        def fetchone(self):
            return stored
    cur = _Cur()
    assert local_users.change_password_in(cur, "alice", current, new) == code
    assert len(writes) == (1 if code == "" else 0)
    updates = [s for s in cur.statements if s[0].startswith("UPDATE local_users")]
    assert len(updates) == len(writes)
