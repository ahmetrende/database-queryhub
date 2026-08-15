"""Approving an access request auto-creates the per-user grant (in the same
transaction as the status flip). These tests drive access_requests.decide()
against a scripted fake connection — no DB."""
from contextlib import contextmanager

import pytest

from queryhub import access_requests as ar


# ---- pure helpers -----------------------------------------------------------

def test_requested_tier_from_column_only():
    assert ar.requested_tier_of({"requested_tier": "rw"}) == "rw"
    assert ar.requested_tier_of({"requested_tier": "DDL"}) == "ddl"


def test_requested_tier_never_trusts_reason_text():
    # SECURITY: the free-text reason must NOT set the tier — a Slack requester
    # writing "[requested tier: ddl]" must still resolve to least-privilege ro.
    assert ar.requested_tier_of({"requested_tier": None,
                                 "reason": "[requested tier: ddl] gimme root"}) == "ro"
    assert ar.requested_tier_of({"reason": "[requested tier: RW] x"}) == "ro"


def test_requested_tier_defaults_ro():
    assert ar.requested_tier_of({"reason": "just let me in"}) == "ro"
    assert ar.requested_tier_of({}) == "ro"
    assert ar.requested_tier_of({"requested_tier": "bogus"}) == "ro"


def test_merge_databases():
    assert ar._merge_databases(["a"], ["b"]) == ["a", "b"]
    assert ar._merge_databases(["a", "b"], ["b"]) == ["a", "b"]
    assert ar._merge_databases(None, ["b"]) is None      # None = all, absorbs
    assert ar._merge_databases(["a"], None) is None


# ---- decide() + _auto_grant against a scripted connection -------------------

class FakeConn:
    """Records every execute; serves scripted fetchone() results in order."""
    def __init__(self, fetch_results):
        self.executed = []          # list of (sql, params)
        self._fetches = list(fetch_results)

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return self._fetches.pop(0)

    def sql_containing(self, fragment):
        return [s for s, _ in self.executed if fragment in s]


ROW = {
    "id": 13, "requester_slack_id": "U0EXAMPLE01", "requester_name": "Dev One",
    "target_server_id": 21, "database_name": "notify_service",
    "attempted_query": None, "reason": "[requested tier: RO] delivery debugging",
    "requested_tier": "ro", "status": "approved",
    "decided_by_slack_id": "U0EXAMPLE99", "decided_by_name": "admin",
    "decision_reason": None, "created_at": None, "decided_at": None,
}


@pytest.fixture
def audit_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(ar.audit, "log_in",
                        lambda cur, rid, aid, aname, action, details=None:
                        calls.append((action, details)))
    return calls


def _wire(monkeypatch, conn):
    @contextmanager
    def fake_txn():
        yield conn
    monkeypatch.setattr(ar.db, "transaction", fake_txn)


def test_approve_creates_grant_fresh(monkeypatch, audit_calls):
    # fetch order: UPDATE..RETURNING row -> SELECT existing grant (None)
    conn = FakeConn([dict(ROW), None])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    ag = out["auto_grant"]
    assert ag == {"applied": True, "reason": "granted",
                  "mode": "ro", "databases": ["notify_service"]}
    assert conn.sql_containing("INSERT INTO user_target_grants")
    assert conn.sql_containing("INSERT INTO requesters")     # whitelist net
    assert audit_calls and audit_calls[0][0] == "access_request_auto_grant"


def test_approve_merges_same_tier(monkeypatch, audit_calls):
    existing = {"mode": "ro", "allowed_databases": ["other_db"], "revoked_at": None}
    conn = FakeConn([dict(ROW), existing])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert out["auto_grant"]["applied"] is True
    assert out["auto_grant"]["databases"] == ["notify_service", "other_db"]


def test_approve_skips_on_tier_conflict(monkeypatch, audit_calls):
    existing = {"mode": "rw", "allowed_databases": None, "revoked_at": None}
    conn = FakeConn([dict(ROW), existing])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    ag = out["auto_grant"]
    assert ag["applied"] is False and ag["reason"] == "tier_conflict"
    # the existing rw grant must NOT be touched
    assert not conn.sql_containing("INSERT INTO user_target_grants")
    assert not audit_calls


def test_approve_revoked_grant_treated_as_fresh(monkeypatch, audit_calls):
    existing = {"mode": "rw", "allowed_databases": ["x"], "revoked_at": "2026-01-01"}
    conn = FakeConn([dict(ROW), existing])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    # revoked rw row is dead — the new ro grant replaces it at the asked tier
    assert out["auto_grant"] == {"applied": True, "reason": "granted",
                                 "mode": "ro", "databases": ["notify_service"]}


def test_approve_no_target_skips(monkeypatch, audit_calls):
    row = dict(ROW, target_server_id=None)
    conn = FakeConn([row])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert out["auto_grant"] == {"applied": False, "reason": "no_target",
                                 "mode": None, "databases": None}
    assert not conn.sql_containing("user_target_grants")


def test_reject_never_grants(monkeypatch, audit_calls):
    row = dict(ROW, status="rejected")
    conn = FakeConn([row])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "rejected", "U0EXAMPLE99", "admin", "no need")
    assert "auto_grant" not in out
    assert not conn.sql_containing("user_target_grants")


def test_already_decided_returns_none(monkeypatch, audit_calls):
    conn = FakeConn([None])
    _wire(monkeypatch, conn)
    assert ar.decide(13, "approved", "U0EXAMPLE99", "admin", None) is None


def test_no_db_grants_whole_target(monkeypatch, audit_calls):
    row = dict(ROW, database_name=None)
    conn = FakeConn([row, None])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert out["auto_grant"]["applied"] is True
    assert out["auto_grant"]["databases"] is None    # all dbs on the target