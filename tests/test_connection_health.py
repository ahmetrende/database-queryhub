"""Periodic connection health (PLA-1592 U7): the rules, the run and the list.

A scheduled job checks each target and keeps one row per target. The admin
connection list shows it as `health`, and a read replica row says whether
read-only runs go to it (`inRotation`). What these tests pin:

- `failingSince` starts at the first failure and stays through the failures
  after it. The next answer clears it.
- A run does not check a target again inside the interval. It never checks an
  excluded engine, a disabled target or a deleted target, and the list never
  shows an old result for one.
- With the switch off, the job does nothing at all. A hung driver costs one
  timeout. Two runs never overlap.
- No password, login name, host or ARN reaches the stored error.

Hermetic: a fake probe, a fake store, no database. The SQL runs against a
real database in tests/test_integration_connection_health.py.
"""
from __future__ import annotations

import contextlib
import importlib.util
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from queryhub import config as cfg
from queryhub import connection_health as ch
from queryhub import connection_probe, replicas, targets
from queryhub.web import admin as web_admin
from queryhub.web import config_admin
from queryhub.web import routes_admin as ra

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
ON = ch.Settings(enabled=True, interval_sec=300, timeout_sec=5)
HOST = "db.example.internal"
USER = "reader_ro"
PASSWORD = "reader_ro-Wq7xK2-not-real"      # the login name is inside it on purpose
ROLE = "arn:aws:iam::123456789012:role/example-archive-reader"
SUPER = {"sub": "U0EXAMPLE001", "name": "Jordan Ray"}


def _target(tid=1, alias="prod-main", engine="postgres", enabled=True,
            deleted_at=None, replica_of=None, engine_config=None) -> dict:
    return {"id": tid, "alias": alias, "engine": engine, "host": HOST,
            "port": 5432, "default_database": "ledger", "enabled": enabled,
            "deleted_at": deleted_at, "replica_of": replica_of,
            "engine_config": engine_config or {}}


class FakeStore:
    """connection_health.Store over a dict. It keeps what a run writes, so the
    next run reads it back, as the table does."""

    def __init__(self, fleet):
        self.targets = {t["id"]: dict(t) for t in fleet}
        self.rows: dict[int, dict] = {}
        self.deleted: list[int] = []

    def load(self):
        out = []
        for tid, t in sorted(self.targets.items()):
            h = self.rows.get(tid) or {}
            out.append({**t, "last_ok": h.get("ok"),
                        "last_checked_at": h.get("checked_at"),
                        "last_failing_since": h.get("failing_since")})
        return out

    def save(self, target_id, outcome, checked_at, failing_since):
        self.rows[target_id] = {"ok": outcome.ok, "checked_at": checked_at,
                                "failing_since": failing_since,
                                "error": outcome.error,
                                "latency_ms": outcome.latency_ms}

    def delete(self, target_ids):
        self.deleted.extend(target_ids)
        return sum(self.rows.pop(t, None) is not None for t in target_ids)


def _stored(at, ok=True):
    return {"ok": ok, "checked_at": at, "failing_since": None if ok else at,
            "error": None if ok else "Connection refused.", "latency_ms": 3}


def _answers(*oks):
    """A check_fn that answers ok/fail in this order, and records each call."""
    seq = iter(oks)
    calls = []

    def check_fn(target, timeout_sec):
        calls.append(target["id"])
        ok = next(seq)
        return ch.Outcome(ok, None if ok else "Connection refused.", 4 if ok else None)
    return check_fn, calls


@pytest.fixture
def config(monkeypatch):
    """bot_config as a dict. A key that is not in it reads as its default."""
    values: dict[str, str] = {}

    def get_setting(key, default=None):
        if key in values:
            return values[key]
        if default is None:
            raise KeyError(key)
        return default
    monkeypatch.setattr(cfg, "get_setting", get_setting)
    return values


def _script():
    spec = importlib.util.spec_from_file_location(
        "check_connection_health", SCRIPTS / "check_connection_health.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- failingSince ------------------------------------------------------------


def test_failing_since_starts_at_the_first_failure_and_ends_at_the_next_answer():
    store = FakeStore([_target()])
    check_fn, _calls = _answers(True, False, False, True)
    seen = []
    for n in range(4):
        now = T0 + timedelta(seconds=300 * n)
        ch.run_once(store, ON, clock=lambda: now, check_fn=check_fn)
        row = store.rows[1]
        seen.append((row["ok"], row["checked_at"], row["failing_since"]))
    first_failure = T0 + timedelta(seconds=300)
    assert seen == [
        (True, T0, None),
        (False, first_failure, first_failure),
        (False, T0 + timedelta(seconds=600), first_failure),   # kept
        (True, T0 + timedelta(seconds=900), None),
    ]


def test_a_first_check_that_fails_starts_the_failure_at_its_own_time():
    store = FakeStore([_target()])
    check_fn, _calls = _answers(False)
    ch.run_once(store, ON, clock=lambda: T0, check_fn=check_fn)
    assert store.rows[1]["failing_since"] == T0
    assert store.rows[1]["error"] == "Connection refused."


# ---- the interval -------------------------------------------------------------


def test_a_target_checked_inside_the_interval_is_not_checked_again():
    store = FakeStore([_target()])
    check_fn, calls = _answers(True, True)
    ch.run_once(store, ON, clock=lambda: T0, check_fn=check_fn)
    summary = ch.run_once(store, ON, clock=lambda: T0 + timedelta(seconds=200),
                          check_fn=check_fn)
    assert calls == [1]
    assert (summary.checked, summary.skipped) == (0, 1)
    assert store.rows[1]["checked_at"] == T0
    # The scheduler's allowance: a result 290 s old is due on a 300 s interval.
    ch.run_once(store, ON, clock=lambda: T0 + timedelta(seconds=290),
                check_fn=check_fn)
    assert calls == [1, 1]


@pytest.mark.parametrize("age, expected", [
    (None, True), (0, False), (269, False), (270, True), (300, True), (4000, True)])
def test_due_allows_a_tenth_of_the_interval_for_the_scheduler(age, expected):
    last = None if age is None else T0 - timedelta(seconds=age)
    assert ch.due(last, T0, 300) is expected


def test_the_allowance_is_at_most_thirty_seconds():
    assert ch.due(T0 - timedelta(seconds=3569), T0, 3600) is False
    assert ch.due(T0 - timedelta(seconds=3570), T0, 3600) is True


# ---- what the check covers ------------------------------------------------------


def test_an_excluded_engine_is_never_checked_and_loses_its_old_result():
    store = FakeStore([_target(1, "prod-main"),
                       _target(2, "events-ch", engine="clickhouse"),
                       _target(3, "archive", engine="athena")])
    store.rows[2] = _stored(T0 - timedelta(hours=1))
    s = ch.Settings(True, 300, 5, ch.parse_engines(" ClickHouse,athena "))
    check_fn, calls = _answers(True)
    summary = ch.run_once(store, s, clock=lambda: T0, check_fn=check_fn)
    assert calls == [1]
    assert 2 not in store.rows and 3 not in store.rows
    assert store.deleted == [2]
    assert summary.deleted == 1


@pytest.mark.parametrize("off", [
    {"enabled": False},
    {"deleted_at": T0 - timedelta(days=2), "enabled": False},
])
def test_a_disabled_or_deleted_target_is_not_checked_and_loses_its_old_result(off):
    store = FakeStore([_target(1), _target(2, "old-db", **off)])
    store.rows[2] = _stored(T0 - timedelta(days=3))
    check_fn, calls = _answers(True)
    ch.run_once(store, ON, clock=lambda: T0, check_fn=check_fn)
    assert calls == [1]
    assert store.deleted == [2] and 2 not in store.rows


def test_scope_says_why_a_target_is_left_out():
    s = ch.Settings(True, 300, 5, frozenset({"athena"}))
    assert ch.scope_reason(_target(), s) is None
    assert ch.scope_reason(_target(enabled=False), s) == "disabled"
    assert ch.scope_reason(_target(enabled=False, deleted_at=T0), s) == "deleted"
    assert ch.scope_reason(_target(engine="athena"), s) == "engine athena is excluded"
    assert ch.scope_reason(_target(), ch.Settings(False, 300, 5)) == "the check is off"


def test_the_exclusion_list_takes_commas_spaces_and_any_case():
    assert ch.parse_engines("ClickHouse, athena  mssql,,") == {
        "clickhouse", "athena", "mssql"}
    assert ch.parse_engines("") == frozenset()
    assert ch.parse_engines(None) == frozenset()


# ---- the settings ---------------------------------------------------------------


def test_the_defaults_check_every_engine_every_five_minutes(config):
    assert ch.settings() == ch.Settings(True, 300, 5, frozenset())


def test_settings_are_read_from_bot_config(config):
    config.update(connection_health_enabled="off",
                  connection_health_interval_sec="120",
                  connection_health_timeout_sec="9",
                  connection_health_exclude_engines="clickhouse")
    assert ch.settings() == ch.Settings(False, 120, 9, frozenset({"clickhouse"}))


@pytest.mark.parametrize("raw, expected", [("0", 1), ("-3", 1), ("999", 60), ("x", 5)])
def test_the_timeout_stays_between_one_and_sixty_seconds(config, raw, expected):
    config["connection_health_timeout_sec"] = raw
    assert ch.settings().timeout_sec == expected


def test_an_interval_that_is_not_a_number_falls_back_to_the_default(config):
    config["connection_health_interval_sec"] = "five minutes"
    assert ch.settings().interval_sec == 300


def test_a_misspelled_engine_is_named_because_it_excludes_nothing(config):
    config["connection_health_exclude_engines"] = "clickhose,athena"
    assert ch.settings().unknown_engines == ["clickhose"]


# ---- one check -------------------------------------------------------------------


def test_a_probe_that_hangs_costs_one_timeout_and_the_run_goes_on(monkeypatch):
    monkeypatch.setattr(ch, "GRACE_SEC", 0)
    monkeypatch.setattr(targets, "get_credentials", lambda tid, mode: (USER, PASSWORD))
    release = threading.Event()

    def probe(engine, host, *a, **k):
        if host == "hangs.example.internal":
            release.wait(10)
        return {"ok": True, "latencyMs": 2, "serverVersion": "17.6", "error": None}
    store = FakeStore([_target(1), {**_target(2, "slow-db"), "host": "hangs.example.internal"}])
    started = time.monotonic()
    try:
        ch.run_once(store, ch.Settings(True, 300, 5), clock=lambda: T0,
                    check_fn=lambda t, timeout: ch.check(t, 0.3, probe=probe))
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 2
    assert store.rows[1]["ok"] is True
    assert store.rows[2] == {"ok": False, "checked_at": T0, "failing_since": T0,
                             "error": "No answer in 0.3 s.", "latency_ms": None}


def test_the_driver_gets_the_timeout_and_the_stored_login(monkeypatch):
    seen = {}
    monkeypatch.setattr(targets, "get_credentials",
                        lambda tid, mode: seen.update(tid=tid, mode=mode) or (USER, PASSWORD))

    def probe(*args, **kw):
        seen["args"], seen["kw"] = args, kw
        return {"ok": True, "latencyMs": 7, "serverVersion": "17.6", "error": None}
    assert ch.check(_target(), 5, probe=probe) == ch.Outcome(True, None, 7)
    assert (seen["tid"], seen["mode"]) == (1, "ro")
    assert seen["args"] == ("postgres", HOST, 5432, "ledger", USER, PASSWORD)
    assert seen["kw"]["timeout_sec"] == 5
    assert set(seen["kw"]["redact"]) == {USER, PASSWORD}
    assert seen["kw"]["application_name"] == ch.APPLICATION_NAME


def test_a_read_replica_is_checked_with_its_primarys_login(monkeypatch):
    asked = []
    monkeypatch.setattr(targets, "get_credentials",
                        lambda tid, mode: asked.append(tid) or (USER, PASSWORD))
    out = ch.check(_target(9, "prod-main-r1", replica_of=1), 5,
                   probe=lambda *a, **k: {"ok": True, "latencyMs": 1,
                                          "serverVersion": None, "error": None})
    assert out.ok is True
    assert asked == [1]


@pytest.mark.parametrize("creds", [LookupError("not configured"),
                                   ("queryhub_ro", targets.SENTINEL_PASSWORD),
                                   ("", "")])
def test_a_target_with_no_stored_login_fails_without_dialling(monkeypatch, creds):
    def get_credentials(tid, mode):
        if isinstance(creds, Exception):
            raise creds
        return creds
    monkeypatch.setattr(targets, "get_credentials", get_credentials)
    out = ch.check(_target(), 5,
                   probe=lambda *a, **k: pytest.fail("dialled with no login"))
    assert out == ch.Outcome(False, ch.NO_CREDENTIALS, None)


def test_a_replica_whose_primary_has_no_login_says_so(monkeypatch):
    monkeypatch.setattr(targets, "get_credentials",
                        lambda tid, mode: ("queryhub_ro", targets.SENTINEL_PASSWORD))
    out = ch.check(_target(9, replica_of=1), 5,
                   probe=lambda *a, **k: pytest.fail("dialled with no login"))
    assert out.error == ch.NO_PRIMARY_CREDENTIALS


def test_athena_is_checked_by_its_catalog_listing_and_the_role_never_shows(monkeypatch):
    from queryhub import schema_catalog
    seen = []

    def listing(target, password):
        seen.append(target.engine_config["role_arn"])
        raise RuntimeError(
            "An error occurred (AccessDenied) when calling the AssumeRole "
            "operation: User: arn:aws:sts::123456789012:assumed-role/gateway/i-0ab "
            f"is not authorized to perform: sts:AssumeRole on resource: {ROLE}")
    monkeypatch.setattr(schema_catalog, "list_target_databases", listing)
    monkeypatch.setattr(targets, "get_credentials",
                        lambda *a: pytest.fail("Athena has no stored login"))
    archive = _target(3, "archive", engine="athena",
                      engine_config={"region": "eu-central-1", "workgroup": "wg",
                                     "database": "events", "role_arn": ROLE})
    out = ch.check(archive, 5,
                   probe=lambda *a, **k: pytest.fail("the probe has no Athena branch"))
    assert seen == [ROLE]
    assert out.ok is False and "AccessDenied" in out.error
    assert "arn:" not in out.error and "123456789012" not in out.error

    monkeypatch.setattr(schema_catalog, "list_target_databases",
                        lambda target, password: ["events"])
    assert ch.check(archive, 5).ok is True


# ---- the stored error -------------------------------------------------------------


def test_a_password_in_the_driver_text_never_reaches_the_table(monkeypatch):
    """The real probe, and a driver that echoes everything that it got."""
    def boom(**kw):
        raise psycopg.OperationalError(
            f'connection to server at "{HOST}", port 5432 failed: FATAL: password '
            f'authentication failed for user "{USER}" (host={HOST} user={USER} '
            f'password={PASSWORD} dbname=ledger) and again {PASSWORD.upper()}')
    monkeypatch.setattr(psycopg, "connect", boom)
    monkeypatch.setattr(targets, "get_credentials", lambda tid, mode: (USER, PASSWORD))
    store = FakeStore([_target()])
    ch.run_once(store, ON, clock=lambda: T0)
    error = store.rows[1]["error"]
    assert "authentication failed" in error
    assert "wq7xk2" not in error.lower() and "not-real" not in error
    assert USER not in error and HOST not in error
    assert len(error) <= ch.ERROR_MAX


@pytest.mark.parametrize("engine, module, name, text", [
    ("clickhouse", "clickhouse_exec", "probe",
     f"Code: 516. DB::Exception: {USER}: Authentication failed: password is "
     f"incorrect, or there is no user with such name. Tried {PASSWORD}."),
    ("mssql", "mssql_exec", "connect",
     f"('28000', \"[28000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
     f"Login failed for user '{USER}'. PWD={PASSWORD}; (18456) (SQLDriverConnect)\")"),
])
def test_no_engine_shows_the_login_or_the_password(monkeypatch, engine,
                                                    module, name, text):
    import importlib
    mod = importlib.import_module(f"queryhub.{module}")

    def boom(*a, **k):
        raise RuntimeError(text)
    monkeypatch.setattr(mod, name, boom)
    monkeypatch.setattr(targets, "get_credentials", lambda tid, mode: (USER, PASSWORD))
    out = ch.check(_target(engine=engine), 5)
    assert out.ok is False
    assert USER not in out.error and "Wq7xK2" not in out.error, out.error


def test_the_host_never_shows_in_a_driver_text_that_scrub_does_not_know(monkeypatch):
    """scrub() knows libpq's shapes of a host. ClickHouse writes it as
    `(host:port)`, so the probe replaces the host that it dialled by value."""
    from queryhub import clickhouse_exec

    def refused(*a, **k):
        raise RuntimeError(f"Code: 210. Connection refused ({HOST}:9440)")
    monkeypatch.setattr(clickhouse_exec, "probe", refused)
    out = connection_probe.probe("clickhouse", HOST, 9440, "default", USER, PASSWORD,
                                 timeout_sec=5)
    assert HOST not in out["error"] and "the host:9440" in out["error"]


def test_the_login_inside_the_password_cannot_leave_half_of_it():
    """The longest value goes first: hiding the login name first would cut
    the password in two and leave its tail."""
    text = connection_probe.hide_values(
        f"failed for {USER} with {PASSWORD}", [USER, PASSWORD])
    assert text == "failed for [redacted] with [redacted]"


def test_a_short_login_is_hidden_as_a_word_only():
    assert connection_probe.hide_values("sa: usage denied", ["sa"]) == \
        "[redacted]: usage denied"


def test_a_password_that_the_masker_would_cut_is_hidden_before_it():
    """The data masker hides an e-mail address in an error. A password with
    one inside would leave the scrubber half masked, and its other half would
    match nothing after. So the probe hides the values before the scrubber."""
    pw = "Wq7xK2-jordan.ray@example.com-tail9"
    out = connection_probe.failure_message(
        RuntimeError(f"authentication failed, tried {pw} on the way"), redact=[pw])
    assert "Wq7xK2" not in out and "tail9" not in out, out
    assert "authentication failed" in out.lower()


def test_a_password_with_a_quote_is_hidden_after_the_driver_text_is_unwrapped():
    """pyodbc gives its message as a tuple repr, which escapes a quote in the
    password. The value does not match the raw text, so the probe hides it
    again after the scrubber unwraps the tuple."""
    pw = "Wq7'xK2-not-real"
    raw = "('28000', " + repr(f'Login failed for "x". Tried {pw} twice.') + ")"
    assert pw not in raw                       # the repr escaped it
    out = connection_probe.failure_message(RuntimeError(raw), redact=[pw])
    assert "Wq7" not in out and "xK2" not in out, out


def test_the_check_hides_the_login_even_when_a_probe_does_not(monkeypatch):
    """Two layers: the probe hides the values, and the check hides them again
    before the table gets the text."""
    monkeypatch.setattr(targets, "get_credentials", lambda tid, mode: (USER, PASSWORD))

    def leaky(*a, **k):
        return {"ok": False, "latencyMs": 3, "serverVersion": None,
                "error": f"refused {USER} with {PASSWORD}"}
    out = ch.check(_target(), 5, probe=leaky)
    assert out.error == "refused [redacted] with [redacted]"


def test_a_credential_store_that_fails_is_a_failed_check_not_a_crash(monkeypatch):
    from queryhub import secrets_providers

    def fail(tid, mode):
        raise secrets_providers.SecretsProviderError(f"secret {ROLE} has no SecretString")
    monkeypatch.setattr(targets, "get_credentials", fail)
    out = ch.check(_target(), 5, probe=lambda *a, **k: pytest.fail("dialled"))
    assert out.ok is False and "SecretString" in out.error
    assert "arn:" not in out.error


def test_the_switch_stays_a_switch_whatever_its_stored_spelling():
    """A guess from the value would read "1" as a number box."""
    assert config_admin._kind("connection_health_enabled", "1") == "bool"
    assert config_admin._kind("connection_health_exclude_engines", "") == "str"


def test_the_stored_error_is_capped_and_carries_no_arn():
    long = "x" * 300 + f" {ROLE}"
    assert len(ch.clean_error(long)) == ch.ERROR_MAX
    assert "arn:" not in ch.clean_error(f"refused: {ROLE} denied")
    assert ch.clean_error(None) == "The check failed."


# ---- the script --------------------------------------------------------------------


@pytest.fixture
def script(monkeypatch):
    mod = _script()
    monkeypatch.setattr(mod.db, "init_pool", lambda **k: None)
    monkeypatch.setattr(connection_probe, "probe",
                        lambda *a, **k: pytest.fail("dialled a target"))
    monkeypatch.setattr(targets, "get_credentials",
                        lambda *a: pytest.fail("read a credential"))
    return mod


def test_with_the_check_off_the_script_exits_at_once(script, config, monkeypatch):
    config["connection_health_enabled"] = "off"
    monkeypatch.setattr(ch, "run_lock", lambda: pytest.fail("took the lock"))
    monkeypatch.setattr(ch.Store, "load", lambda self: pytest.fail("read the fleet"))
    assert script.main([]) == 0


def test_a_run_that_finds_the_lock_taken_checks_nothing(script, config, monkeypatch):
    @contextlib.contextmanager
    def taken():
        yield False
    monkeypatch.setattr(ch, "run_lock", taken)
    monkeypatch.setattr(ch, "run_once", lambda *a, **k: pytest.fail("ran without the lock"))
    assert script.main([]) == 0


def test_a_run_that_holds_the_lock_checks(script, config, monkeypatch):
    ran = []

    @contextlib.contextmanager
    def held():
        yield True
    monkeypatch.setattr(ch, "run_lock", held)
    monkeypatch.setattr(ch, "run_once", lambda store, s: ran.append(s) or ch.Summary())
    assert script.main([]) == 0
    assert ran == [ch.Settings(True, 300, 5)]


def test_the_dry_run_prints_the_plan_and_changes_nothing(script, config, monkeypatch,
                                                         capsys):
    fleet = [{**_target(1, "prod-main"), "last_ok": None, "last_checked_at": None,
              "last_failing_since": None},
             {**_target(2, "old-db", enabled=False), "last_ok": True,
              "last_checked_at": T0, "last_failing_since": None}]
    monkeypatch.setattr(ch.Store, "load", lambda self: fleet)
    monkeypatch.setattr(ch.Store, "save", lambda *a: pytest.fail("wrote a result"))
    monkeypatch.setattr(ch.Store, "delete", lambda *a: pytest.fail("deleted a result"))
    monkeypatch.setattr(ch, "run_lock", lambda: pytest.fail("took the lock"))
    assert script.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "check   prod-main (postgres): never checked" in out
    assert "skip    old-db (postgres): disabled" in out
    assert "delete  old-db" in out
    assert "1 to check, 1 to skip, 1 stored result(s) to delete. Nothing was changed." in out


# ---- GET /admin/connections --------------------------------------------------------


def _admin_row(tid, alias, *, engine="postgres", enabled=True, deleted_at=None,
               replica_of=None, replica_of_alias=None) -> dict:
    return {"id": tid, "alias": alias, "host": HOST, "port": 5432,
            "default_database": "ledger", "enabled": enabled, "notes": None,
            "engine": engine, "secrets_provider": "local", "tags": {},
            "replica_of": replica_of, "replica_of_alias": replica_of_alias,
            "deleted_at": deleted_at, "deleted_reason": None,
            "super_admin_only": False,
            "credentials": {m: {"username": None, "configured": False,
                                "placeholder": False} for m in ("ro", "rw", "ddl")}}


def _health(tid, ok=True, at=T0, error=None) -> dict:
    return {"target_server_id": tid, "ok": ok, "checked_at": at,
            "failing_since": None if ok else at - timedelta(minutes=10),
            "error": error}


@pytest.fixture
def listing(monkeypatch, config):
    """The connection list over a fake database that answers by statement."""
    state = {
        "rows": [_admin_row(1, "prod-main"),
                 _admin_row(2, "old-db", enabled=False),
                 _admin_row(3, "gone-db", enabled=False, deleted_at=T0),
                 _admin_row(4, "events-ch", engine="clickhouse"),
                 _admin_row(5, "never-checked"),
                 _admin_row(6, "prod-main-r1", replica_of=1, replica_of_alias="prod-main"),
                 _admin_row(7, "prod-main-r2", enabled=False, replica_of=1,
                            replica_of_alias="prod-main"),
                 _admin_row(8, "sales-mssql", engine="mssql"),
                 _admin_row(9, "sales-mssql-r1", engine="mssql", replica_of=8,
                            replica_of_alias="sales-mssql")],
        "health": [_health(1), _health(2), _health(3), _health(4),
                   _health(6, ok=False, error="Connection refused.")],
        "statements": [], "fail_health": False}
    monkeypatch.setattr(web_admin.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(web_admin.admins, "is_super_admin", lambda uid: True)
    monkeypatch.setattr(ra.targets, "list_admin_rows", lambda: state["rows"])

    def fetch_all(sql, params=None):
        state["statements"].append(sql)
        if "FROM connection_health" in sql:
            if state["fail_health"]:
                raise psycopg.OperationalError("relation does not exist")
            ids = set(params[0])
            return [h for h in state["health"] if h["target_server_id"] in ids]
        if "replica_of = ANY" in sql:
            primaries = set(params[0])
            return [{"id": r["id"], "alias": r["alias"], "replica_of": r["replica_of"]}
                    for r in state["rows"]
                    if r["replica_of"] in primaries and r["enabled"]]
        return []                                     # the owners
    monkeypatch.setattr(ra.db, "fetch_all", fetch_all)
    from queryhub.web import routes_data
    monkeypatch.setattr(routes_data, "_catalog_databases_map", lambda tids: {})
    return state


def _rows_by_alias(state):
    return {c["id"]: c for c in ra.admin_connections(claims=SUPER)["connections"]}


def test_a_checked_target_shows_its_last_result(listing):
    got = _rows_by_alias(listing)
    assert got["prod-main"]["health"] == {
        "ok": True, "checkedAt": T0.isoformat(), "failingSince": None, "error": None}
    assert got["prod-main-r1"]["health"] == {
        "ok": False, "checkedAt": T0.isoformat(),
        "failingSince": (T0 - timedelta(minutes=10)).isoformat(),
        "error": "Connection refused."}


def test_no_old_result_shows_beside_a_target_the_check_does_not_cover(listing, config):
    config["connection_health_exclude_engines"] = "clickhouse"
    got = _rows_by_alias(listing)
    for alias in ("old-db", "gone-db", "events-ch", "never-checked"):
        assert got[alias]["health"] is None, alias


def test_with_the_check_off_no_row_shows_a_result_and_nothing_is_read(listing, config):
    config["connection_health_enabled"] = "off"
    got = _rows_by_alias(listing)
    assert all(c["health"] is None for c in got.values())
    assert not [s for s in listing["statements"] if "connection_health" in s]


def test_the_whole_fleet_is_read_in_one_statement(listing):
    _rows_by_alias(listing)
    assert len([s for s in listing["statements"] if "FROM connection_health" in s]) == 1


def test_a_failed_health_read_still_answers_the_list(listing):
    listing["fail_health"] = True
    got = _rows_by_alias(listing)
    assert len(got) == 9
    assert all(c["health"] is None for c in got.values())


def test_a_replica_row_says_whether_reads_go_to_it(listing, config):
    config["replica_routing"] = "on"
    got = _rows_by_alias(listing)
    assert got["prod-main-r1"]["inRotation"] is True
    assert got["prod-main-r2"]["inRotation"] is False      # disabled
    assert got["sales-mssql-r1"]["inRotation"] is False    # not a PostgreSQL primary
    assert got["prod-main"]["inRotation"] is None          # not a replica


def test_with_routing_off_no_replica_is_in_rotation(listing):
    got = _rows_by_alias(listing)
    assert got["prod-main-r1"]["inRotation"] is False
    assert not [s for s in listing["statements"] if "replica_of = ANY" in s]


def test_the_list_only_gains_the_two_fields(listing):
    """Additive: every key a row had before is still there, unchanged."""
    entry = _rows_by_alias(listing)["prod-main"]
    before = ra._connection_payload(listing["rows"][0], [])
    before["owners"] = []
    assert {k: v for k, v in entry.items() if k not in ("health", "inRotation")} == before


# ---- replicas.in_rotation ----------------------------------------------------------


def _fleet_rows():
    return [{"id": 1, "engine": "postgres", "replica_of": None},
            {"id": 6, "engine": "postgres", "replica_of": 1},
            {"id": 8, "engine": "mssql", "replica_of": None},
            {"id": 9, "engine": "mssql", "replica_of": 8}]


def test_rotation_reuses_the_routing_rule(config, monkeypatch):
    config["replica_routing"] = "on"
    asked = []
    monkeypatch.setattr(replicas, "enabled_by_primary",
                        lambda ids: asked.append(ids) or {1: [{"id": 6, "alias": "r1"}]})
    assert replicas.in_rotation(_fleet_rows()) == {6: True, 9: False}
    assert asked == [[1]]          # a SQL Server primary is never asked about


def test_rotation_is_off_for_every_replica_while_routing_is_off(config, monkeypatch):
    monkeypatch.setattr(replicas, "enabled_by_primary",
                        lambda ids: pytest.fail("read while routing is off"))
    assert replicas.in_rotation(_fleet_rows()) == {6: False, 9: False}


def test_a_failed_rotation_read_gives_no_answer(config, monkeypatch):
    config["replica_routing"] = "on"

    def boom(ids):
        raise psycopg.OperationalError("the metadata database is gone")
    monkeypatch.setattr(replicas, "enabled_by_primary", boom)
    assert replicas.in_rotation(_fleet_rows()) == {}


def test_routing_and_rotation_share_the_engine_rule(config, monkeypatch):
    """`choose` and `in_rotation` ask the same `routes_reads`: a read on a
    SQL Server primary never goes to a replica, even with routing on."""
    config["replica_routing"] = "on"
    monkeypatch.setattr(replicas, "replicas_of", lambda pid: [
        {"id": 9, "alias": "sales-mssql-r1", "host": "r1.example.internal", "port": 1433}])
    monkeypatch.setattr(replicas, "_wrote_recently", lambda *a: False)
    monkeypatch.setattr(replicas, "health", lambda *a: replicas.Health(True, 0.0))
    request = {"id": 1, "query": "SELECT 1", "requester_slack_id": "U0EXAMPLE001"}
    sales = type("T", (), {"id": 8, "alias": "sales-mssql", "engine": "mssql"})()
    assert replicas.choose(sales, request, "ro", USER, PASSWORD).route is None
    main = type("T", (), {"id": 8, "alias": "prod-main", "engine": "postgres"})()
    assert replicas.choose(main, request, "ro", USER, PASSWORD).route is not None
    assert replicas.routes_reads("mssql") is False and replicas.routes_reads(None) is True


def test_a_fleet_with_no_replica_asks_nothing(monkeypatch):
    monkeypatch.setattr(replicas, "enabled", lambda: pytest.fail("asked"))
    assert replicas.in_rotation([{"id": 1, "engine": "postgres", "replica_of": None}]) == {}


# ---- the System configuration screen ----------------------------------------------


KEYS = {"connection_health_enabled": "bool", "connection_health_interval_sec": "int",
        "connection_health_timeout_sec": "int",
        "connection_health_exclude_engines": "str"}


def test_the_four_keys_show_typed_in_their_own_group(monkeypatch):
    seeded = {"connection_health_enabled": "on", "connection_health_interval_sec": "300",
              "connection_health_timeout_sec": "5",
              "connection_health_exclude_engines": ""}
    monkeypatch.setattr(config_admin.db, "fetch_all", lambda sql, params=None: [
        {"key": k, "value": v, "description": "d", "updated_at": None}
        for k, v in seeded.items()])
    groups = {g["id"]: g for g in config_admin.build_config()["groups"]}
    assert [(i["key"], i["type"]) for i in groups["health"]["items"]] == list(KEYS.items())
    assert "other" not in groups


@pytest.mark.parametrize("key", ["connection_health_interval_sec",
                                 "connection_health_timeout_sec"])
def test_zero_is_refused_for_the_interval_and_the_timeout(key):
    assert config_admin._coerce("0", "int", "300", key=key) is None
    assert config_admin._coerce("60", "int", "300", key=key) == "60"


def test_the_four_keys_are_seeded_with_the_defaults_the_code_passes():
    text = (Path(__file__).resolve().parent.parent / "migrations"
            / "191_connection_health_settings.sql").read_text(encoding="utf-8")
    for key, default in (("connection_health_enabled", "on"),
                         ("connection_health_interval_sec", str(ch.DEFAULT_INTERVAL_SEC)),
                         ("connection_health_timeout_sec", str(ch.DEFAULT_TIMEOUT_SEC)),
                         ("connection_health_exclude_engines", "")):
        assert f"('{key}', '{default}'," in text, key
