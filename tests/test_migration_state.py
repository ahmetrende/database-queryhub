"""The pending-migration check: its verdicts, and when it says "unknown".

A wrong "pending" makes a healthy install unready (/readyz answers 503). So
each doubt must give None and stay quiet: no migrations directory, no ledger
table, a login that cannot read the ledger, a ledger of another program, no
pool, a database error. The tests also cover the real verdicts, and the
agreement with scripts/apply_migrations.py about what is pending.
"""
import contextlib
import importlib.util
import logging
from pathlib import Path

import psycopg
import pytest

from queryhub import migration_state

ROOT = Path(__file__).resolve().parent.parent


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "apply_migrations", ROOT / "scripts" / "apply_migrations.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class FakeLedger:
    """db.fetch_one / db.fetch_all as the ledger read sees them."""

    def __init__(self, readable=True, versions=(), error=None, select_error=None):
        self.readable = readable
        self.versions = list(versions)
        self.error = error
        self.select_error = select_error
        self.queries = []

    def fetch_one(self, sql, params=None):
        self.queries.append(sql)
        if self.error:
            raise self.error
        return {"readable": self.readable}

    def fetch_all(self, sql, params=None):
        self.queries.append(sql)
        if self.select_error:
            raise self.select_error
        return [{"version": v} for v in self.versions]


@pytest.fixture
def ledger(monkeypatch):
    def install(**kwargs):
        fake = FakeLedger(**kwargs)
        monkeypatch.setattr(migration_state.db, "pool_ready", lambda: True)
        monkeypatch.setattr(migration_state.db, "fetch_one", fake.fetch_one)
        monkeypatch.setattr(migration_state.db, "fetch_all", fake.fetch_all)
        return fake
    return install


SHIPPED = ["001_initial_schema.sql", "002_b.sql", "003_c.sql"]


def _migrations(tmp_path, names=SHIPPED):
    d = tmp_path / "migrations"
    d.mkdir(parents=True)
    for n in names:
        (d / n).write_text(f"-- {n}\n", encoding="utf-8")
    return d


def _loud(caplog):
    return [r for r in caplog.records
            if r.name == migration_state.log.name and r.levelno > logging.DEBUG]


# ---- verdicts ---------------------------------------------------------------

def test_files_missing_from_the_ledger_are_pending_in_apply_order(ledger):
    ledger(versions=["001_initial_schema.sql"])
    assert migration_state.pending(SHIPPED) == ["002_b.sql", "003_c.sql"]


def test_nothing_is_pending_when_the_ledger_has_every_file(ledger):
    ledger(versions=SHIPPED)
    assert migration_state.pending(SHIPPED) == []


def test_a_ledger_row_without_a_file_is_not_pending(ledger):
    """The database is newer than the code, for example after a rollback of
    the code. The running code has what it needs."""
    ledger(versions=SHIPPED + ["004_newer.sql"])
    assert migration_state.pending(SHIPPED) == []


def test_an_empty_ledger_makes_every_file_pending(ledger):
    """The runner makes the ledger before the first file. A failed first run
    leaves it empty, and then every file is truly pending."""
    ledger(versions=[])
    assert migration_state.pending(SHIPPED) == SHIPPED


# ---- doubts give None, quietly ------------------------------------------------

@pytest.mark.parametrize("kwargs,ledger_selects", [
    ({"readable": None}, 0),     # no schema_migrations table
    ({"readable": False}, 0),    # split roles: the runtime has no access
    ({"select_error": psycopg.errors.InsufficientPrivilege("denied")}, 1),
    ({"error": psycopg.OperationalError("server closed the connection")}, 0),
    ({"versions": ["20240101120000"]}, 1),   # another program's ledger
])
def test_each_doubt_gives_unknown_and_stays_quiet(ledger, caplog, kwargs, ledger_selects):
    caplog.set_level(logging.DEBUG, logger=migration_state.log.name)
    fake = ledger(**kwargs)
    assert migration_state.pending(SHIPPED) is None
    assert sum(q.startswith("SELECT version FROM") for q in fake.queries) == ledger_selects
    assert _loud(caplog) == []
    migration_state.warn_if_pending()
    assert _loud(caplog) == []


def test_no_pool_gives_unknown_without_a_query(monkeypatch):
    """A pool that failed at boot is not opened again: that waits 30 s."""
    monkeypatch.setattr(migration_state.db, "pool_ready", lambda: False)

    def no_query(*a, **k):
        raise AssertionError("the check queried without a pool")

    monkeypatch.setattr(migration_state.db, "fetch_one", no_query)
    monkeypatch.setattr(migration_state.db, "fetch_all", no_query)
    assert migration_state.pending(SHIPPED) is None


def test_no_migrations_directory_gives_unknown(monkeypatch, ledger, caplog):
    caplog.set_level(logging.DEBUG, logger=migration_state.log.name)
    fake = ledger(versions=[])
    monkeypatch.setattr(migration_state, "find_directory", lambda: None)
    assert migration_state.shipped_versions() is None
    assert migration_state.pending(migration_state.shipped_versions()) is None
    migration_state.warn_if_pending()
    assert fake.queries == []
    assert _loud(caplog) == []


# ---- where the files are --------------------------------------------------------

def _fake_package(tmp_path, monkeypatch):
    """Point the module at src/pkg/ inside a fake checkout."""
    pkg = tmp_path / "checkout" / "src" / "pkg"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(migration_state, "__file__", str(pkg / "migration_state.py"))
    return tmp_path / "checkout"


def test_the_checkout_directory_comes_first(tmp_path, monkeypatch):
    checkout = _fake_package(tmp_path, monkeypatch)
    in_checkout = _migrations(checkout)
    elsewhere = tmp_path / "cwd"
    _migrations(elsewhere)
    monkeypatch.chdir(elsewhere)
    assert migration_state.find_directory().resolve() == in_checkout.resolve()


def test_the_working_directory_serves_an_installed_package(tmp_path, monkeypatch):
    """The container image: the package is in site-packages, and the files
    are in /app/migrations, the working directory."""
    _fake_package(tmp_path, monkeypatch)
    image_root = tmp_path / "app"
    in_cwd = _migrations(image_root)
    monkeypatch.chdir(image_root)
    assert migration_state.find_directory().resolve() == in_cwd.resolve()


def test_a_directory_without_the_first_migration_belongs_to_another_program(
        tmp_path, monkeypatch):
    _fake_package(tmp_path, monkeypatch)
    other = tmp_path / "other"
    _migrations(other, names=["0001_create_users.sql"])
    monkeypatch.chdir(other)
    assert migration_state.find_directory() is None


def test_the_check_and_the_runner_find_the_same_files_in_this_checkout():
    runner = _load_runner()
    assert migration_state.find_directory().resolve() == runner.MIGRATIONS_DIR.resolve()
    # The runner's own historical rule: sorted *.sql, the file name is the version.
    expected = [f.name for f in sorted(runner.MIGRATIONS_DIR.glob("*.sql"))]
    assert migration_state.shipped_versions() == expected
    assert expected[0] == migration_state.FIRST_FILE


class _RunnerCursor:
    def __init__(self, ledger):
        self.ledger = ledger
        self._one = None
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self._one = {"ok": True} if "to_regclass" in sql else None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return [{"version": v, "checksum": c} for v, c in self.ledger.items()]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RunnerConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass


def test_the_runner_and_the_check_agree_on_what_is_pending(tmp_path, monkeypatch,
                                                         capsys, ledger):
    runner = _load_runner()
    directory = _migrations(tmp_path)
    recorded = {"001_initial_schema.sql": runner._checksum(
        (directory / "001_initial_schema.sql").read_text())}
    cursor = _RunnerCursor(recorded)

    @contextlib.contextmanager
    def connection(_migrator):
        yield _RunnerConnection(cursor)

    monkeypatch.setattr(runner, "MIGRATIONS_DIR", directory)
    monkeypatch.setattr(runner, "_connection", connection)
    monkeypatch.delenv("BOT_DB_MIGRATOR_USER", raising=False)
    assert runner.main(["--dry-run"]) == 0
    would_apply = [line.split(": ", 1)[1] for line in capsys.readouterr().out.splitlines()
                   if line.startswith("would apply: ")]

    ledger(versions=list(recorded))
    assert would_apply == ["002_b.sql", "003_c.sql"]
    assert migration_state.pending(migration_state.shipped_versions(directory)) == would_apply
    # A dry run reads and writes nothing else.
    assert not [q for q in cursor.executed if q.lstrip().upper().startswith(("INSERT", "CREATE"))]


# ---- the startup line ---------------------------------------------------------

def test_the_startup_line_has_the_count_the_first_five_names_and_the_command(
        monkeypatch, caplog):
    names = [f"{n:03d}_m.sql" for n in range(144, 151)]
    monkeypatch.setattr(migration_state, "pending", lambda shipped: names)
    caplog.set_level(logging.DEBUG, logger=migration_state.log.name)
    migration_state.warn_if_pending()
    loud = _loud(caplog)
    assert [r.levelno for r in loud] == [logging.CRITICAL]
    assert loud[0].getMessage() == (
        "7 migrations pending: 144_m.sql, 145_m.sql, 146_m.sql, 147_m.sql, "
        "148_m.sql and 2 more. Apply them with: python scripts/apply_migrations.py")


def test_the_startup_line_never_raises(monkeypatch):
    """A log line must never decide whether a service starts."""
    def broken(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(migration_state, "find_directory", broken)
    assert migration_state.shipped_versions() is None
    monkeypatch.setattr(migration_state, "pending", broken)
    migration_state.warn_if_pending()


@pytest.mark.parametrize("verdict", [[], None])
def test_the_startup_line_is_silent_when_nothing_is_pending_or_unknown(
        monkeypatch, caplog, verdict):
    monkeypatch.setattr(migration_state, "pending", lambda shipped: verdict)
    caplog.set_level(logging.DEBUG, logger=migration_state.log.name)
    migration_state.warn_if_pending()
    assert _loud(caplog) == []


# ---- the readiness cache ------------------------------------------------------

def test_the_check_keeps_a_verdict_for_its_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(migration_state, "pending",
                        lambda shipped: calls.append(shipped) or ["144_a.sql"])
    now = [1000.0]
    check = migration_state.PendingCheck(ttl_seconds=30, clock=lambda: now[0])
    assert check.get() == ["144_a.sql"]
    now[0] += 29.9
    assert check.get() == ["144_a.sql"]
    assert len(calls) == 1
    now[0] += 0.1
    check.get()
    assert len(calls) == 2


def test_the_check_reads_the_file_list_once_at_the_start(tmp_path, monkeypatch, ledger):
    """A `git pull` after the start adds files that the running code does
    not use, so they must not make this process unready."""
    directory = _migrations(tmp_path, names=SHIPPED[:2])
    monkeypatch.setattr(migration_state, "find_directory", lambda: directory)
    ledger(versions=SHIPPED[:2])
    check = migration_state.PendingCheck(ttl_seconds=0)
    (directory / "003_c.sql").write_text("-- new\n", encoding="utf-8")
    assert check.get() == []
    assert migration_state.pending(migration_state.shipped_versions()) == ["003_c.sql"]
