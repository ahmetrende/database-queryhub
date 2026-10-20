"""A database that is gone leaves the schema catalog.

The hourly refresh only snapshots the databases that exist, and nothing removed
the rows of one that was dropped, so it stayed in the sidebar for good with its
tables (a dropped database sat there for two weeks). The refresh now removes the
catalog rows of a database that the server no longer lists, and these tests pin
the guards that make a wrong answer cost nothing: a failed or empty listing, a
plan that would empty the target, a run that names one database, and engines
that are read in their own way.
"""
from __future__ import annotations

import contextlib
import importlib.util
import pathlib

import pytest

from queryhub import schema_catalog as sc
from queryhub import targets

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _script():
    spec = importlib.util.spec_from_file_location(
        "_refresh_schema_catalog", ROOT / "scripts" / "refresh_schema_catalog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _target(engine="postgres", tid=7, alias="example-rds"):
    return targets.TargetServer(
        id=tid, alias=alias, host="example.internal", port=5432,
        default_database="postgres", username="u", enabled=True, notes=None,
        engine=engine, engine_config=None)


# ---- the pure plan --------------------------------------------------------

def test_a_database_the_server_no_longer_lists_is_planned_for_removal():
    assert sc.plan_prune(["a", "b", "old"], ["a", "b"]) == ["old"]


def test_nothing_is_planned_when_everything_is_still_there():
    assert sc.plan_prune(["a", "b"], ["a", "b", "new"]) == []


def test_an_empty_live_list_proves_nothing():
    """A login that sees no database is a permission problem."""
    assert sc.plan_prune(["a", "b"], []) == []
    assert sc.plan_prune(["a", "b"], None) == []


def test_a_plan_that_would_empty_the_target_is_refused():
    assert sc.plan_prune(["a", "b"], ["c"]) == []


def test_one_survivor_is_enough_to_prune_the_rest():
    assert sc.plan_prune(["a", "b", "c"], ["a"]) == ["b", "c"]


# ---- the removal is one transaction with its own audit row ----------------

class _Cur:
    def __init__(self):
        self.sql, self.rowcount = [], 0

    def execute(self, sql, args=None):
        self.sql.append((" ".join(sql.split()), args))
        self.rowcount = {"schema_tables": 46, "schema_functions": 120}[
            "schema_tables" if "schema_tables" in sql else "schema_functions"]


def test_prune_deletes_tables_and_functions_and_writes_one_audit_row(monkeypatch):
    cur, audits = _Cur(), []

    @contextlib.contextmanager
    def fake_tx():
        yield cur

    monkeypatch.setattr(sc.db, "transaction", fake_tx)
    monkeypatch.setattr(sc.audit, "log_in", lambda c, *a: audits.append((c, a)))
    out = sc.prune_databases(_target(), ["old_db"])
    assert out == {"tables": 46, "functions": 120}
    assert [s for s, _ in cur.sql][0].startswith("DELETE FROM schema_tables")
    assert [s for s, _ in cur.sql][1].startswith("DELETE FROM schema_functions")
    assert all(args == (7, ["old_db"]) for _, args in cur.sql)
    assert len(audits) == 1 and audits[0][0] is cur       # same transaction
    action, details = audits[0][1][3], audits[0][1][4]
    assert action == "schema_catalog_pruned"
    assert details["databases"] == ["old_db"] and details["tables"] == 46


def test_an_empty_plan_touches_nothing(monkeypatch):
    monkeypatch.setattr(sc.db, "transaction",
                        lambda: pytest.fail("no transaction for an empty plan"))
    assert sc.prune_databases(_target(), []) == {"tables": 0, "functions": 0}


# ---- the refresh uses it, behind the guards -------------------------------

@pytest.fixture
def refresh(monkeypatch):
    mod = _script()
    calls = {"pruned": [], "snapshots": []}
    monkeypatch.setattr(mod, "_ro_password", lambda tid: "pw")
    monkeypatch.setattr(mod.schema_catalog, "list_snapshot_databases",
                        lambda tid: ["a", "old"])
    monkeypatch.setattr(mod.schema_catalog, "prune_databases",
                        lambda t, dbs: calls["pruned"].append(list(dbs)) or {"tables": 3, "functions": 1})
    monkeypatch.setattr(mod.schema_catalog, "snapshot_database",
                        lambda t, pw, d: calls["snapshots"].append(d) or (1, 2))
    mod.calls = calls
    return mod


def test_a_full_refresh_prunes_the_vanished_database_and_still_snapshots_the_rest(
        refresh, monkeypatch):
    monkeypatch.setattr(refresh.schema_catalog, "list_target_databases",
                        lambda t, pw: ["a"])
    summary = refresh.refresh_target(_target())
    assert refresh.calls["pruned"] == [["old"]]
    assert refresh.calls["snapshots"] == ["a"]
    assert list(summary) == ["a"], "the prune adds nothing to the health summary"


def test_a_failed_listing_removes_nothing(refresh, monkeypatch):
    def boom(t, pw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(refresh.schema_catalog, "list_target_databases", boom)
    assert refresh.refresh_target(_target()) == {"*": "unreachable: RuntimeError"}
    assert refresh.calls["pruned"] == []


def test_a_run_that_names_one_database_removes_nothing(refresh, monkeypatch):
    monkeypatch.setattr(refresh.schema_catalog, "list_target_databases",
                        lambda t, pw: ["a", "b"])
    refresh.refresh_target(_target(), only_database="a")
    assert refresh.calls["pruned"] == []


@pytest.mark.parametrize("engine", ["athena", "clickhouse"])
def test_engines_that_are_read_in_their_own_way_are_left_alone(refresh, monkeypatch, engine):
    monkeypatch.setattr(refresh.schema_catalog, "list_target_databases",
                        lambda t, pw: ["a"])
    monkeypatch.setattr(refresh.engines, "spec",
                        lambda e: type("S", (), {"requires_credentials": False})())
    refresh.refresh_target(_target(engine=engine))
    assert refresh.calls["pruned"] == []


def test_a_prune_that_fails_does_not_stop_the_snapshots(refresh, monkeypatch):
    monkeypatch.setattr(refresh.schema_catalog, "list_target_databases",
                        lambda t, pw: ["a"])

    def boom(t, dbs):
        raise RuntimeError("bot db blip")
    monkeypatch.setattr(refresh.schema_catalog, "prune_databases", boom)
    summary = refresh.refresh_target(_target())
    assert refresh.calls["snapshots"] == ["a"] and "a" in summary


def test_the_report_changes_nothing(refresh, monkeypatch, capsys):
    monkeypatch.setattr(refresh.schema_catalog, "list_target_databases",
                        lambda t, pw: ["a"])
    assert refresh.prune_report([_target()]) == 0
    out = capsys.readouterr().out
    assert "example-rds: would remove old" in out and "1 database(s) would be removed" in out
    assert refresh.calls["pruned"] == [] and refresh.calls["snapshots"] == []
