"""A query whose tier has no login on the target is refused at submit.

The executor connects with the login of the request's tier. Three live
targets had no RW or DDL login while every screen showed DDL on them
(2026-11-12), so such a request reached an approver, was approved, and failed
at execution. Submit now refuses it first, with the executor's own words.

Nothing here may reach a database. A read a test forgot to fake fails it.
"""
from __future__ import annotations

import pytest

from queryhub import access, auto_approve, core_submit as cs, db, lifecycle, targets

USER = "U0EXAMPLE002"
DDL_EVERYWHERE = {"mode": "ddl", "allowed_databases": None, "source": "admin_or_bypass"}
LEDGER = targets.TargetServer(
    id=7, alias="example-pg", host="pg.example.test", port=5432, default_database="ledger",
    username="reader", enabled=True, notes=None, engine="postgres", engine_config={})


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)


# --- which tiers have a login ----------------------------------------------

def _row(tid, provider="local", engine="postgres", ro=True, rw=False, ddl=False):
    return {"id": tid, "provider": provider, "engine": engine, "ro": ro, "rw": rw, "ddl": ddl}


# Taken at import, before the conftest fixture stubs it for every test.
REAL_CONFIGURED_TIERS = targets.configured_tiers


@pytest.fixture
def rows(monkeypatch):
    """The real `configured_tiers` over faked rows."""
    st = {"rows": []}
    monkeypatch.setattr(targets, "configured_tiers", REAL_CONFIGURED_TIERS)
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None: st["rows"])
    return st


def test_a_local_target_has_only_the_tiers_with_a_login(rows):
    rows["rows"] = [_row(1), _row(2, rw=True), _row(3, rw=True, ddl=True)]
    got = targets.configured_tiers([1, 2, 3])
    assert got == {1: {"ro"}, 2: {"ro", "rw"}, 3: {"ro", "rw", "ddl"}}


def test_another_provider_counts_every_tier(rows):
    """Only the local provider can be read from the row."""
    rows["rows"] = [_row(4, provider="aws-sm", ro=False)]
    assert targets.configured_tiers([4]) == {4: {"ro", "rw", "ddl"}}


def test_an_engine_without_stored_logins_counts_every_tier(rows):
    rows["rows"] = [_row(5, engine="athena", ro=False)]
    assert targets.configured_tiers([5]) == {5: {"ro", "rw", "ddl"}}


def test_no_ids_reads_nothing(rows):
    rows["rows"] = None             # a read would fail on this
    assert targets.configured_tiers([]) == {}


# --- submit ------------------------------------------------------------------

@pytest.fixture
def submit(monkeypatch):
    st = {"tiers": {"ro"}}
    monkeypatch.setattr(access, "roles", lambda pid: [])
    monkeypatch.setattr(auto_approve, "active_grants", lambda pid, at=None: [])
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.requesters, "open_request_count", lambda uid: 0)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(cs.targets, "get", lambda tid: LEDGER)
    monkeypatch.setattr(cs.teams, "effective_grant_for_user", lambda uid, tid: DDL_EVERYWHERE)
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "ddl")
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(cs.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(cs.targets, "configured_tiers", lambda ids: {i: st["tiers"] for i in ids})

    def go(sql):
        return cs.validate_submission(
            USER, "Ex", target_server_id=LEDGER.id, database_name="ledger",
            query=sql, justification="moving the month-end report", confirmed=True)
    st["go"] = go
    return st


@pytest.mark.parametrize("sql,tier", [("UPDATE ledgers SET n = 1 WHERE id = 2", "RW"),
                                      ("CREATE TABLE ledgers_copy (id int)", "DDL")])
def test_a_tier_without_a_login_is_refused_at_submit(submit, sql, tier):
    out = submit["go"](sql)
    assert isinstance(out, cs.Rejection), out
    assert out.reason == "no_credential"
    assert f"not ready for {tier} queries" in out.message and "example-pg" in out.message


def test_a_read_still_goes_through(submit):
    out = submit["go"]("SELECT * FROM ledgers")
    assert isinstance(out, cs.Prepared) and out.required_mode == "ro"


def test_a_write_goes_through_when_the_login_exists(submit):
    submit["tiers"] = {"ro", "rw"}
    out = submit["go"]("UPDATE ledgers SET n = 1 WHERE id = 2")
    assert isinstance(out, cs.Prepared) and out.required_mode == "rw"
