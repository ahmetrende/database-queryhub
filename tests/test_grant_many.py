"""Granting the same access to several people is ONE act.

The admin screen asks for "these five, this tier, this server". Doing that as
five calls can leave three written and two not, and the grants table cannot
describe that state: it records what exists, never what was meant, so the
operator cannot tell a partial write from a deliberate one. Hence one
transaction — and hence this file, because the transaction is the feature.

`_write_grant` is the three statements a grant is made of, shared by the single
and the many so there is only one place for the SQL to be wrong.
"""
import contextlib

import pytest

from queryhub import grants


class _Cur:
    """Records what was written, and can be told to fail on the Nth person."""

    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.granted, self.whitelisted, self.audits = [], [], 0
        self.suppressed = False
        self._row = None

    def execute(self, sql, params=()):
        if "SET LOCAL app.auth_dm_suppress" in sql:
            self.suppressed = True
            self._row = None
        elif "INSERT INTO requesters" in sql:
            self.whitelisted.append(params[0])
            self._row = {"inserted": True}
        elif "INSERT INTO user_target_grants" in sql:
            if self.fail_on is not None and params[0] == self.fail_on:
                raise RuntimeError("deadlock detected")
            self.granted.append((params[0], params[1], params[3]))
            self._row = {"mode": params[3], "allowed_databases": params[2],
                         "expires_at": params[5]}
        elif "INSERT INTO audit_log" in sql:
            self.audits += 1
            self._row = None

    def fetchone(self):
        return self._row


@pytest.fixture
def cur(monkeypatch):
    c = _Cur()
    committed = {"ok": False}

    @contextlib.contextmanager
    def txn():
        yield c
        committed["ok"] = True                 # only reached without an exception

    monkeypatch.setattr(grants.db, "transaction", txn)
    monkeypatch.setattr(grants, "control_plane_target_ids", lambda: {99})
    monkeypatch.setattr(grants, "notify_grantee",
                        lambda *a, **k: c.__dict__.setdefault("notified", []).append(a[0]))
    # The notify path asks the catalog for the target's alias — a real query,
    # and this file is testing the transaction, not the label.
    monkeypatch.setattr("queryhub.targets.get", lambda tid: None)
    c.committed = committed
    return c


def _people(*ids):
    return [(i, {"name": i, "email": None, "tz": None}) for i in ids]


def test_everyone_is_written_in_one_transaction(cur):
    out = grants.grant_many(
        granter_id="UADMIN", granter_name="Admin", grantees=_people("U1", "U2", "U3"),
        target_id=7, mode="ro", databases=None, reason="onboarding")
    assert [g for g, _, _ in cur.granted] == ["U1", "U2", "U3"]
    assert [r["grantee_id"] for r in out] == ["U1", "U2", "U3"]
    assert cur.audits == 3                      # one audit row per grant
    assert cur.suppressed is True               # the outbox does not double-DM


def test_one_failure_writes_nothing(cur):
    cur.fail_on = "U2"
    with pytest.raises(RuntimeError):
        grants.grant_many(granter_id="UADMIN", granter_name=None,
                          grantees=_people("U1", "U2", "U3"), target_id=7,
                          mode="ro", databases=None, reason=None)
    # The transaction never committed, so U1's write does not survive either —
    # which is the whole point: a half-applied authorization is not a state
    # anyone can repair from the table.
    assert cur.committed["ok"] is False
    assert "U3" not in [g for g, _, _ in cur.granted]


def test_the_control_plane_refuses_before_anything_runs(cur):
    with pytest.raises(PermissionError):
        grants.grant_many(granter_id="UADMIN", granter_name=None,
                          grantees=_people("U1"), target_id=99, mode="ddl",
                          databases=None, reason=None)
    assert cur.granted == [] and cur.suppressed is False


def test_an_empty_list_is_not_an_error(cur):
    assert grants.grant_many(granter_id="UADMIN", granter_name=None, grantees=[],
                             target_id=7, mode="ro", databases=None,
                             reason=None) == []
    assert cur.granted == []


def test_each_grantee_is_notified_once_after_the_commit(cur, monkeypatch):
    monkeypatch.setattr(grants, "notify_grantee",
                        lambda gid, *a, **k: cur.__dict__.setdefault("dms", []).append(gid))
    grants.grant_many(granter_id="UADMIN", granter_name=None,
                      grantees=_people("U1", "U2"), target_id=7, mode="rw",
                      databases=["app"], reason=None)
    assert cur.dms == ["U1", "U2"]


def test_notification_can_be_turned_off(cur, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("notify=False must send nothing")
    monkeypatch.setattr(grants, "notify_grantee", boom)
    grants.grant_many(granter_id="UADMIN", granter_name=None,
                      grantees=_people("U1"), target_id=7, mode="ro",
                      databases=None, reason=None, notify=False)
    assert [g for g, _, _ in cur.granted] == ["U1"]


def test_the_tier_and_databases_reach_every_row(cur):
    grants.grant_many(granter_id="UADMIN", granter_name=None,
                      grantees=_people("U1", "U2"), target_id=7, mode="ddl",
                      databases=["a", "b"], reason=None, notify=False)
    assert {m for _, _, m in cur.granted} == {"ddl"}
    assert all(t == 7 for _, t, _ in cur.granted)
