"""Approving an access request auto-creates the per-user grant (in the same
transaction as the status flip). These tests drive access_requests.decide()
against a scripted fake connection — no DB."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

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


@pytest.fixture(autouse=True)
def _ordinary_target(monkeypatch):
    """`_auto_grant` asks `grants.control_plane_target_ids()` whether the
    target is the bot's own metadata database, and that reads `bot_config`.
    These tests forbid a real connection, so answer it here: the fixture
    targets in this file are ordinary ones.

    Autouse and empty-set rather than per-test, so the next test added to this
    file gets the same answer instead of a confusing connection error."""
    monkeypatch.setattr("queryhub.grants.control_plane_target_ids",
                        lambda: set())


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

    def params_of(self, fragment):
        """Parameters of the one statement that contains `fragment`."""
        hits = [p for s, p in self.executed if fragment in s]
        assert len(hits) == 1, hits
        return hits[0]


ROW = {
    "id": 13, "requester_slack_id": "U0EXAMPLE01", "requester_name": "Dev One",
    "target_server_id": 21, "database_name": "shipping_service",
    "attempted_query": None, "reason": "[requested tier: RO] delivery debugging",
    "requested_tier": "ro", "status": "approved",
    "decided_by_slack_id": "U0EXAMPLE99", "decided_by_name": "admin",
    "decision_reason": None, "created_at": None, "decided_at": None,
}

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
_PAST = _NOW - timedelta(days=3)
_FUTURE = _NOW + timedelta(days=4)


def _grant(mode="ro", dbs=None, expires_at=None, active=True):
    """The row the SELECT in `_auto_grant` returns for an existing grant."""
    return {"mode": mode, "allowed_databases": dbs,
            "expires_at": expires_at, "active": active}


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
    assert ag == {"applied": True, "reason": "granted", "mode": "ro",
                  "databases": ["shipping_service"], "expires_at": None}
    assert conn.sql_containing("INSERT INTO user_target_grants")
    assert conn.sql_containing("INSERT INTO requesters")     # whitelist net
    assert audit_calls and audit_calls[0][0] == "access_request_auto_grant"
    assert audit_calls[0][1]["expires_at"] is None


def test_approve_merges_same_tier(monkeypatch, audit_calls):
    conn = FakeConn([dict(ROW), _grant(dbs=["other_db"])])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert out["auto_grant"]["applied"] is True
    # `sorted(set(existing) | set(new))` -- the union is sorted, so the order
    # here follows the names, not the order they arrived in.
    assert out["auto_grant"]["databases"] == ["other_db", "shipping_service"]


def test_approve_skips_on_tier_conflict(monkeypatch, audit_calls):
    conn = FakeConn([dict(ROW), _grant(mode="rw")])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    ag = out["auto_grant"]
    assert ag["applied"] is False and ag["reason"] == "tier_conflict"
    # the existing rw grant must NOT be touched
    assert not conn.sql_containing("INSERT INTO user_target_grants")
    assert not audit_calls


def test_approve_revoked_grant_treated_as_fresh(monkeypatch, audit_calls):
    # A revoked row is not active, whatever its tier or end date says.
    conn = FakeConn([dict(ROW), _grant(mode="rw", dbs=["x"], active=False)])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    # revoked rw row is dead — the new ro grant replaces it at the asked tier
    assert out["auto_grant"] == {"applied": True, "reason": "granted",
                                 "mode": "ro", "databases": ["shipping_service"],
                                 "expires_at": None}


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


# ---- end dates: a lapsed grant is dead, an active limit stays ---------------
#
# The upsert used to leave `expires_at` alone. Someone whose grant had lapsed
# asked again, was approved, was told "your access is active", and was still
# refused: the row came back to life with its old end date in the past.


def test_approve_after_a_lapse_starts_without_the_old_end_date(monkeypatch,
                                                               audit_calls):
    conn = FakeConn([dict(ROW), _grant(expires_at=_PAST, active=False)])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert out["auto_grant"]["applied"] is True
    assert out["auto_grant"]["expires_at"] is None
    # (uid, target, databases, tier, granted_by, expires_at)
    params = conn.params_of("INSERT INTO user_target_grants")
    assert params[-1] is None
    assert audit_calls[0][1]["expires_at"] is None


def test_a_lapsed_grant_does_not_block_another_tier(monkeypatch, audit_calls):
    # The lapsed row is rw, the request asks ro. A lapsed grant gives nothing,
    # so it must not raise "an active grant at a different tier exists".
    conn = FakeConn([dict(ROW), _grant(mode="rw", expires_at=_PAST, active=False)])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert out["auto_grant"]["applied"] is True
    assert out["auto_grant"]["mode"] == "ro"


def test_a_lapsed_grant_is_replaced_not_merged(monkeypatch, audit_calls):
    conn = FakeConn([dict(ROW), _grant(dbs=["old_db"], expires_at=_PAST,
                                       active=False)])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert out["auto_grant"]["databases"] == ["shipping_service"]


def test_an_active_limit_is_kept_not_removed(monkeypatch, audit_calls):
    # The admin gave this person one week. An approval of a second request must
    # neither lengthen that nor drop it.
    conn = FakeConn([dict(ROW), _grant(dbs=["other_db"], expires_at=_FUTURE)])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert out["auto_grant"]["expires_at"] == _FUTURE
    assert conn.params_of("INSERT INTO user_target_grants")[-1] == _FUTURE
    assert audit_calls[0][1]["expires_at"] == _FUTURE.isoformat()


def test_active_means_the_same_as_the_readers_say(monkeypatch, audit_calls):
    """The test for "active" is made by the database on its own clock, with the
    rule the grant readers use: not revoked, and no end date or one ahead."""
    conn = FakeConn([dict(ROW), None])
    _wire(monkeypatch, conn)
    ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    select = conn.sql_containing("FROM user_target_grants")[0]
    assert "revoked_at IS NULL" in select
    assert "expires_at IS NULL OR expires_at > NOW()" in select


def test_the_upsert_names_expires_at(monkeypatch, audit_calls):
    """If `expires_at` is missing from the DO UPDATE list, a stale end date
    survives a re-grant. That was the bug."""
    conn = FakeConn([dict(ROW), None])
    _wire(monkeypatch, conn)
    ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    upsert = conn.sql_containing("INSERT INTO user_target_grants")[0]
    assert "expires_at = EXCLUDED.expires_at" in upsert    # FakeConn folds spaces
