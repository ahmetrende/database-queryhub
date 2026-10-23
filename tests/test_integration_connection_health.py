"""Connection health on a real database: the table, the run and the list.

The tests in tests/test_connection_health.py pin the rules with a fake store.
Only a database shows that the SQL keeps them: the migrations run twice, the
upsert keeps one row per target through ok, fail, fail, ok, the CHECKs refuse
an inconsistent row, a target that leaves the check loses its row, two runs
never overlap, and GET /admin/connections reads `health` and `inRotation`.

The fixture is a primary, two read replicas of it and a disabled target, which
no deployment has. Each test makes them again and deletes them at the end. The
run reads only the fixture's targets, so a stray run against a live database
writes nothing for a real one.
"""
from __future__ import annotations

import contextlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from queryhub import config as cfg
from queryhub import connection_health as ch
from queryhub import db
from queryhub.web import admin as web_admin
from queryhub.web import routes_admin as ra

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
PRIMARY = "health-fixture-primary"
REPLICA = "health-fixture-replica"
REPLICA_OFF = "health-fixture-replica-off"
DISABLED = "health-fixture-disabled"
ALIASES = [PRIMARY, REPLICA, REPLICA_OFF, DISABLED]
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
ON = ch.Settings(enabled=True, interval_sec=300, timeout_sec=5)
SUPER = {"sub": "U0EXAMPLE001", "name": "Jordan Ray"}


def _wipe() -> None:
    ids = [r["id"] for r in db.fetch_all(
        "SELECT id FROM target_servers WHERE alias = ANY(%s)", (ALIASES,))]
    if ids:
        db.execute("DELETE FROM target_servers WHERE id = ANY(%s)", (ids,))


def _target(alias: str, *, enabled: bool = True, replica_of: int | None = None) -> int:
    return db.insert_returning(
        "INSERT INTO target_servers (alias, host, default_database, username, "
        "  password_encrypted, enabled, replica_of) "
        "VALUES (%s, 'fixture.example.test', 'postgres', 'unused', 'unused', "
        "  %s, %s) RETURNING id", (alias, enabled, replica_of))["id"]


@pytest.fixture
def fleet():
    db.init_pool()
    _wipe()
    primary = _target(PRIMARY)
    ids = {"primary": primary,
           "replica": _target(REPLICA, replica_of=primary),
           "replica_off": _target(REPLICA_OFF, enabled=False, replica_of=primary),
           "disabled": _target(DISABLED, enabled=False)}
    yield ids
    _wipe()


class FixtureStore(ch.Store):
    """The real store, reading only the fixture's targets."""

    def __init__(self, ids):
        self.ids = set(ids.values())

    def load(self):
        return [r for r in super().load() if r["id"] in self.ids]


def _row(target_id: int) -> dict | None:
    return db.fetch_one(
        "SELECT ok, checked_at, failing_since, error, latency_ms "
        "  FROM connection_health WHERE target_server_id = %s", (target_id,))


def _answers(*oks):
    seq = iter(oks)

    def check_fn(target, timeout_sec):
        ok = next(seq)
        return ch.Outcome(ok, None if ok else "Connection refused.", 9 if ok else None)
    return check_fn


@contextlib.contextmanager
def _owner():
    """A connection that may run DDL: the pool's login, or the migrator when
    the metadata roles are split (docs/OPERATIONS.md §28)."""
    user = os.environ.get("BOT_DB_MIGRATOR_USER")
    if not user:
        with db.connection() as conn:
            yield conn
        return
    from queryhub.config import ENV
    with psycopg.connect(host=ENV.bot_db_host, port=ENV.bot_db_port,
                         dbname=ENV.bot_db_name, user=user,
                         password=os.environ.get("BOT_DB_MIGRATOR_PASSWORD", "")) as conn:
        conn.execute("SELECT set_config('role', %s, false)",
                     (os.environ["BOT_DB_OWNER_ROLE"],))
        yield conn


def _settings_rows() -> dict[str, str]:
    return {r["key"]: r["value"] for r in db.fetch_all(
        "SELECT key, value FROM bot_config WHERE key LIKE 'connection_health_%%'")}


def test_both_migrations_run_a_second_time_and_change_nothing():
    db.init_pool()
    before = _settings_rows()
    assert set(before) == {"connection_health_enabled", "connection_health_interval_sec",
                           "connection_health_timeout_sec",
                           "connection_health_exclude_engines"}
    db.execute("UPDATE bot_config SET value = '120' "
               " WHERE key = 'connection_health_interval_sec'")
    try:
        with _owner() as conn:
            for name in ("190_connection_health.sql",
                         "191_connection_health_settings.sql"):
                conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
            conn.commit()
        # A value set by hand is kept, not reset to the seed.
        assert _settings_rows() == {**before, "connection_health_interval_sec": "120"}
        assert db.fetch_one("SELECT count(*) AS n FROM pg_class "
                            " WHERE relname = 'connection_health'")["n"] == 1
    finally:
        db.execute("UPDATE bot_config SET value = %s "
                   " WHERE key = 'connection_health_interval_sec'",
                   (before["connection_health_interval_sec"],))


def test_migration_192_fills_an_empty_seed_and_keeps_a_chosen_value():
    key = "connection_health_exclude_engines"
    db.init_pool()
    before = _settings_rows()[key]
    sql = (MIGRATIONS / "192_connection_health_skip_clickhouse.sql").read_text(encoding="utf-8")

    def run():
        with _owner() as conn:
            conn.execute(sql)
            conn.commit()

    try:
        db.execute("UPDATE bot_config SET value = '' WHERE key = %s", (key,))
        run()
        assert _settings_rows()[key] == "clickhouse"
        run()
        assert _settings_rows()[key] == "clickhouse"
        db.execute("UPDATE bot_config SET value = 'athena' WHERE key = %s", (key,))
        run()
        assert _settings_rows()[key] == "athena"
    finally:
        db.execute("UPDATE bot_config SET value = %s WHERE key = %s", (before, key))


def test_one_row_per_target_through_ok_fail_fail_ok(fleet):
    store = FixtureStore({"primary": fleet["primary"]})
    check_fn = _answers(True, False, False, True)
    seen = []
    for n in range(4):
        now = T0 + timedelta(seconds=300 * n)
        ch.run_once(store, ON, clock=lambda: now, check_fn=check_fn)
        row = _row(fleet["primary"])
        seen.append((row["ok"], row["checked_at"], row["failing_since"], row["error"]))
    first_failure = T0 + timedelta(seconds=300)
    assert seen == [
        (True, T0, None, None),
        (False, first_failure, first_failure, "Connection refused."),
        (False, T0 + timedelta(seconds=600), first_failure, "Connection refused."),
        (True, T0 + timedelta(seconds=900), None, None),
    ]
    assert db.fetch_one("SELECT count(*) AS n FROM connection_health "
                        " WHERE target_server_id = %s", (fleet["primary"],))["n"] == 1


def test_a_run_inside_the_interval_writes_nothing(fleet):
    store = FixtureStore({"primary": fleet["primary"]})
    ch.run_once(store, ON, clock=lambda: T0, check_fn=_answers(True))
    summary = ch.run_once(store, ON, clock=lambda: T0 + timedelta(seconds=60),
                          check_fn=lambda t, s: pytest.fail("checked inside the interval"))
    assert summary.checked == 0
    assert _row(fleet["primary"])["checked_at"] == T0


@pytest.mark.parametrize("ok, failing_since, error", [
    (True, T0, None),
    (False, None, "refused"),
    (False, T0, None),
    (True, None, "refused"),
    (False, T0, "x" * 201),
], ids=["an-answer-is-not-failing", "a-failure-says-since-when",
        "a-failure-says-why", "an-answer-has-no-error", "the-error-is-capped"])
def test_the_checks_refuse_an_inconsistent_row(fleet, ok, failing_since, error):
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction() as cur:
            cur.execute("INSERT INTO connection_health (target_server_id, ok, "
                        "  checked_at, failing_since, error) VALUES (%s, %s, %s, %s, %s)",
                        (fleet["primary"], ok, T0, failing_since, error))


def test_a_target_that_leaves_the_check_loses_its_row(fleet):
    store = FixtureStore(fleet)
    ch.run_once(store, ON, clock=lambda: T0,
                check_fn=lambda t, s: ch.Outcome(True, None, 2))
    for key in ("primary", "replica"):
        assert _row(fleet[key]) is not None, key
    for key in ("replica_off", "disabled"):
        assert _row(fleet[key]) is None, key
    db.execute("UPDATE target_servers SET enabled = FALSE WHERE id = %s",
               (fleet["replica"],))
    summary = ch.run_once(store, ON, clock=lambda: T0 + timedelta(seconds=10),
                          check_fn=lambda t, s: pytest.fail("nothing is due"))
    assert summary.deleted == 1
    assert _row(fleet["replica"]) is None
    assert _row(fleet["primary"]) is not None


def test_a_deleted_target_takes_its_row_with_it(fleet):
    ch.Store().save(fleet["disabled"], ch.Outcome(False, "refused", None), T0, T0)
    db.execute("DELETE FROM target_servers WHERE id = %s", (fleet["disabled"],))
    assert _row(fleet["disabled"]) is None


def test_two_runs_never_overlap():
    db.init_pool()
    with ch.run_lock() as first:
        assert first is True
        with ch.run_lock() as second:
            assert second is False
    with ch.run_lock() as again:
        assert again is True


# ---- GET /admin/connections ---------------------------------------------------------


_KEYS = ("replica_routing", "connection_health_enabled",
         "connection_health_exclude_engines")


@pytest.fixture
def real_config(monkeypatch):
    """bot_config read from the database, not the conftest's defaults. The
    keys a test changes start at their seeds and get their old values back."""
    def get_setting(key, default=None):
        row = db.fetch_one("SELECT value FROM bot_config WHERE key = %s", (key,))
        if row is None:
            if default is None:
                raise KeyError(key)
            return default
        return row["value"]
    monkeypatch.setattr(cfg, "get_setting", get_setting)
    monkeypatch.setattr(web_admin.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(web_admin.admins, "is_super_admin", lambda uid: True)
    before = {r["key"]: r["value"] for r in db.fetch_all(
        "SELECT key, value FROM bot_config WHERE key = ANY(%s)", (list(_KEYS),))}
    for key, seed in zip(_KEYS, ("off", "on", "")):
        db.execute("UPDATE bot_config SET value = %s WHERE key = %s", (seed, key))
    yield
    for key, value in before.items():
        db.execute("UPDATE bot_config SET value = %s WHERE key = %s", (value, key))


def _listed() -> dict:
    return {c["id"]: c for c in ra.admin_connections(claims=SUPER)["connections"]
            if c["id"] in ALIASES}


def test_the_list_reads_health_and_rotation(fleet, real_config):
    store = ch.Store()
    store.save(fleet["primary"], ch.Outcome(True, None, 12), T0, None)
    store.save(fleet["replica"], ch.Outcome(False, "Connection refused.", None),
               T0, T0 - timedelta(minutes=5))
    # Old rows beside targets that the check no longer covers.
    store.save(fleet["replica_off"], ch.Outcome(True, None, 3), T0, None)
    store.save(fleet["disabled"], ch.Outcome(True, None, 3), T0, None)
    db.execute("UPDATE bot_config SET value = 'on' WHERE key = 'replica_routing'")

    got = _listed()
    assert got[PRIMARY]["health"] == {"ok": True, "checkedAt": T0.isoformat(),
                                      "failingSince": None, "error": None}
    assert got[REPLICA]["health"] == {
        "ok": False, "checkedAt": T0.isoformat(),
        "failingSince": (T0 - timedelta(minutes=5)).isoformat(),
        "error": "Connection refused."}
    assert got[REPLICA_OFF]["health"] is None
    assert got[DISABLED]["health"] is None
    assert got[REPLICA]["inRotation"] is True
    assert got[REPLICA_OFF]["inRotation"] is False
    assert got[PRIMARY]["inRotation"] is None and got[DISABLED]["inRotation"] is None

    db.execute("UPDATE bot_config SET value = 'off' WHERE key = 'replica_routing'")
    assert _listed()[REPLICA]["inRotation"] is False


def test_with_the_check_off_the_list_shows_no_result(fleet, real_config):
    ch.Store().save(fleet["primary"], ch.Outcome(True, None, 12), T0, None)
    assert _listed()[PRIMARY]["health"] is not None
    db.execute("UPDATE bot_config SET value = 'off' WHERE key = 'connection_health_enabled'")
    assert _listed()[PRIMARY]["health"] is None
