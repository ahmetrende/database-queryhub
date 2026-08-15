"""SEC-ENG: the approval scope check derives the request tier with the
target's engine, and prefers the tier persisted at submit.

The old bug: admins._scope_admits called query_safety.required_mode()
with no engine, so a non-Postgres query was classified with the
Postgres parser and could be admitted below its true tier.
"""
import dba_slack_bot.query_safety as qs
import dba_slack_bot.targets as targets
from dba_slack_bot import admins


def test_persisted_tier_is_preferred_over_reparsing(monkeypatch):
    # If the row carries the tier computed at submit, use it verbatim —
    # never re-parse. A plain SELECT text must not downgrade a stored 'ddl'.
    called = {"n": 0}
    monkeypatch.setattr(qs, "required_mode",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "ro")
    assert admins._request_tier(
        {"required_tier": "ddl", "query": "SELECT 1", "engine": "postgres"}
    ) == "ddl"
    assert called["n"] == 0  # persisted value short-circuits the re-parse


def test_engine_is_threaded_from_the_request(monkeypatch):
    seen = {}
    monkeypatch.setattr(qs, "required_mode",
                        lambda sql, engine="postgres": seen.update(engine=engine) or "rw")
    admins._request_tier({"query": "MERGE INTO t ...", "engine": "mssql"})
    assert seen["engine"] == "mssql"


def test_engine_resolved_from_target_when_absent(monkeypatch):
    # Legacy row: no persisted tier, no engine column. The engine must be
    # looked up from the target rather than defaulting to Postgres.
    seen = {}
    monkeypatch.setattr(qs, "required_mode",
                        lambda sql, engine="postgres": seen.update(engine=engine) or "rw")
    monkeypatch.setattr(targets, "get", lambda tid: type("T", (), {"enabled": True, "engine": "mssql"})())
    admins._request_tier({"query": "MERGE INTO t ...", "target_server_id": 7})
    assert seen["engine"] == "mssql"


def test_engine_defaults_to_postgres_without_any_hint(monkeypatch):
    seen = {}
    monkeypatch.setattr(qs, "required_mode",
                        lambda sql, engine="postgres": seen.update(engine=engine) or "ro")
    admins._request_tier({"query": "SELECT 1"})
    assert seen["engine"] == "postgres"


def _scope(max_tier):
    return {"max_tier": max_tier, "scope_target_ids": None, "scope_team_ids": None}


def test_ro_admin_cannot_admit_a_write_request(monkeypatch):
    # required_tier persisted as 'rw' — an RO-scoped admin must be refused.
    monkeypatch.setattr(qs, "required_mode", lambda *a, **k: "ro")  # must not be used
    req = {"required_tier": "rw", "query": "SELECT 1"}
    assert admins._scope_admits(_scope("ro"), req) is False
    assert admins._scope_admits(_scope("rw"), req) is True
    assert admins._scope_admits(_scope("ddl"), req) is True


def test_ro_admin_admits_ro_request():
    req = {"required_tier": "ro", "query": "SELECT 1"}
    assert admins._scope_admits(_scope("ro"), req) is True
