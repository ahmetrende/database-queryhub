"""bot_config reads are cached for a few seconds, and writes invalidate.

~90 call sites read bot_config, several per request: the live fleet showed over
a million sequential scans of a 61-row table. That is not an index problem
(Postgres is right to seq-scan 61 rows) — it is one round trip per read. The TTL
is deliberately small so the documented contract, "runtime-effective, no restart
needed", still holds.
"""
import pytest

from dba_slack_bot import config as cfg


@pytest.fixture(autouse=True)
def no_db_config():
    """Override conftest's global stub.

    conftest replaces cfg.get_setting for every test so nothing touches a DB.
    These tests are ABOUT get_setting, so they need the real one — the DB read
    underneath is monkeypatched per test instead.
    """
    yield


def test_second_read_does_not_hit_the_database(monkeypatch):
    cfg.invalidate_cache()
    calls = []

    def fake_fetch_one(sql, params=None):
        calls.append(params)
        return {"value": "300"}

    import dba_slack_bot.db as dbmod
    monkeypatch.setattr(dbmod, "fetch_one", fake_fetch_one)
    assert cfg.get_setting("query_timeout_sec", "1") == "300"
    assert cfg.get_setting("query_timeout_sec", "1") == "300"
    assert cfg.get_setting("query_timeout_sec", "1") == "300"
    assert len(calls) == 1, f"expected 1 DB read, got {len(calls)}"


def test_invalidate_forces_a_reread(monkeypatch):
    cfg.invalidate_cache()
    values = iter(["off", "on"])
    import dba_slack_bot.db as dbmod
    monkeypatch.setattr(dbmod, "fetch_one",
                        lambda sql, params=None: {"value": next(values)})
    assert cfg.get_setting("kill_switch", "off") == "off"
    cfg.invalidate_cache()
    assert cfg.get_setting("kill_switch", "off") == "on"


def test_missing_key_still_uses_the_default_and_is_cached(monkeypatch):
    cfg.invalidate_cache()
    calls = []
    import dba_slack_bot.db as dbmod

    def fake(sql, params=None):
        calls.append(1)
        return None
    monkeypatch.setattr(dbmod, "fetch_one", fake)
    assert cfg.get_setting("nope", "fallback") == "fallback"
    assert cfg.get_setting("nope", "fallback") == "fallback"
    assert len(calls) == 1, "a missing key should be cached too, not re-queried"


def test_ttl_is_short_enough_to_stay_runtime_effective():
    # The kill switch must not sit stale; seconds, not minutes.
    assert 0 < cfg._CACHE_TTL_SECONDS <= 10
