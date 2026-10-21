"""A read replica holds no catalog rows.

The hourly refresh reads `targets.list_enabled()`, which leaves a replica out on
purpose. A target that became a replica after its last snapshot therefore kept
its rows for good, and search could show them next to the primary's. The
whole-fleet refresh now removes them and writes one audit row per replica.

These tests pin the plumbing: what one transaction holds, which runs may clean,
and that a failure never costs the refresh. The same SQL runs on a real server
in tests/test_integration_replica_catalog.py.
"""
from __future__ import annotations

import contextlib
import importlib.util
import logging
import pathlib

import pytest

from queryhub import schema_catalog as sc
from queryhub import targets

ROOT = pathlib.Path(__file__).resolve().parent.parent

REPLICA = {"id": 9, "alias": "example-replica", "primary_alias": "example-rds",
           "tables": 3, "functions": 1}


def _script():
    spec = importlib.util.spec_from_file_location(
        "_refresh_schema_catalog_replica", ROOT / "scripts" / "refresh_schema_catalog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _target(engine="postgres", tid=7, alias="example-rds", host="example.internal"):
    return targets.TargetServer(
        id=tid, alias=alias, host=host, port=5432, default_database="postgres",
        username="u", enabled=True, notes=None, engine=engine, engine_config=None)


# ---- the listing ----------------------------------------------------------

def test_only_a_replica_that_holds_rows_is_listed(monkeypatch):
    rows = [dict(REPLICA),
            {**REPLICA, "id": 10, "alias": "example-empty", "tables": 0, "functions": 0},
            {**REPLICA, "id": 11, "alias": "example-routines", "tables": 0, "functions": 2}]
    seen = []
    monkeypatch.setattr(sc.db, "fetch_all",
                        lambda sql, params=None: seen.append(" ".join(sql.split())) or rows)
    assert [r["alias"] for r in sc.replicas_holding_catalog()] == [
        "example-replica", "example-routines"]
    assert "t.replica_of IS NOT NULL" in seen[0], "a primary is never listed"


# ---- one replica is one transaction with its own audit row ----------------

class _Cur:
    """Answers the clean-up's statements in the order it asks them."""

    def __init__(self, columns=12, databases=("app", "ledger"), tables=3, functions=1):
        self.sql, self.rowcount = [], 0
        self._columns, self._databases = columns, databases
        self._deleted = {"schema_tables": tables, "schema_functions": functions}

    def execute(self, sql, args=None):
        text = " ".join(sql.split())
        self.sql.append((text, args))
        for table, count in self._deleted.items():
            if text.startswith(f"DELETE FROM {table} "):
                self.rowcount = count

    def fetchone(self):
        return {"n": self._columns}

    def fetchall(self):
        return [{"database_name": d} for d in self._databases]


@pytest.fixture
def one_transaction(monkeypatch):
    state = {"cur": _Cur(), "audits": [], "transactions": 0}

    @contextlib.contextmanager
    def fake_tx():
        state["transactions"] += 1
        yield state["cur"]

    monkeypatch.setattr(sc.db, "transaction", fake_tx)
    monkeypatch.setattr(sc.audit, "log_in", lambda c, *a: state["audits"].append((c, a)))
    return state


def test_a_replica_loses_its_rows_in_one_transaction_with_one_audit_row(one_transaction):
    out = sc._clear_replica(REPLICA)
    assert out == {"tables": 3, "columns": 12, "functions": 1}
    cur = one_transaction["cur"]
    assert one_transaction["transactions"] == 1
    deletes = [s for s, _ in cur.sql if s.startswith("DELETE")]
    assert [d.split()[2] for d in deletes] == ["schema_tables", "schema_functions"]
    assert len(one_transaction["audits"]) == 1
    assert one_transaction["audits"][0][0] is cur        # same transaction
    action, details = one_transaction["audits"][0][1][3:5]
    assert action == "schema_catalog_pruned"
    assert details.pop("why").startswith("read replica")
    assert details == {"target": "example-replica", "targetId": 9,
                       "replicaOf": "example-rds", "databases": ["app", "ledger"],
                       "tables": 3, "columns": 12, "functions": 1}


def test_both_deletes_name_the_replica_condition(one_transaction):
    """A wrong id must never remove the rows of a target that people can pick."""
    sc._clear_replica(REPLICA)
    deletes = [s for s, _ in one_transaction["cur"].sql if s.startswith("DELETE")]
    assert len(deletes) == 2
    assert all("t.replica_of IS NOT NULL" in d for d in deletes)


def test_a_replica_with_nothing_left_to_remove_writes_no_audit_row(one_transaction):
    one_transaction["cur"] = _Cur(tables=0, functions=0)
    assert sc._clear_replica(REPLICA) is None
    assert one_transaction["audits"] == []


def test_every_replica_that_holds_rows_is_cleared(monkeypatch):
    second = {**REPLICA, "id": 12, "alias": "example-replica-two"}
    cleared = []
    monkeypatch.setattr(sc, "replicas_holding_catalog", lambda: [REPLICA, second])
    monkeypatch.setattr(
        sc, "_clear_replica",
        lambda r: cleared.append(r["id"]) or {"tables": 1, "columns": 2, "functions": 0})
    out = sc.clear_replica_catalog()
    assert cleared == [9, 12]
    assert [o["alias"] for o in out] == ["example-replica", "example-replica-two"]
    assert out[0] == {"id": 9, "alias": "example-replica", "primary_alias": "example-rds",
                      "tables": 1, "columns": 2, "functions": 0}


def test_a_replica_that_lost_nothing_is_not_reported(monkeypatch):
    monkeypatch.setattr(sc, "replicas_holding_catalog", lambda: [REPLICA])
    monkeypatch.setattr(sc, "_clear_replica", lambda r: None)
    assert sc.clear_replica_catalog() == []


def test_nothing_held_means_no_transaction(monkeypatch):
    monkeypatch.setattr(sc, "replicas_holding_catalog", lambda: [])
    monkeypatch.setattr(sc.db, "transaction",
                        lambda: pytest.fail("no transaction when nothing is held"))
    assert sc.clear_replica_catalog() == []


# ---- the refresh script: which runs clean, and what a failure costs -------

@pytest.fixture
def refresh(monkeypatch):
    mod = _script()
    mod.cleared, mod.refreshed = [], []
    monkeypatch.setattr(mod.schema_catalog, "clear_replica_catalog",
                        lambda: mod.cleared.append(1) or [dict(REPLICA)])
    monkeypatch.setattr(mod.schema_catalog, "replicas_holding_catalog",
                        lambda: [dict(REPLICA)])
    fleet = [_target()]
    monkeypatch.setattr(mod.targets_mod, "list_enabled", lambda: fleet)
    monkeypatch.setattr(mod, "refresh_target",
                        lambda t, only_database=None: mod.refreshed.append(t.alias) or {})
    monkeypatch.setattr(mod, "refresh_archive_freshness", lambda only_alias=None: 0)
    monkeypatch.setattr(mod.schema_catalog, "summary_is_ok", lambda s: (True, None))
    monkeypatch.setattr(mod.schema_catalog, "record_refresh",
                        lambda *a: {"alert": False, "recovered": False})
    monkeypatch.setattr(mod, "_announce", lambda n: None)
    return mod


@pytest.fixture
def run(refresh, monkeypatch):
    def go(*argv):
        monkeypatch.setattr("sys.argv", ["refresh", *argv])
        return refresh.main()
    return go


def test_the_hourly_run_clears_the_replicas_and_says_so(refresh, run, caplog):
    with caplog.at_level(logging.INFO, logger="refresh_schema_catalog"):
        assert run() == 0
    assert refresh.cleared == [1] and refresh.refreshed == ["example-rds"]
    assert ("example-replica: removed the catalog rows of this read replica "
            "(3 tables, 1 functions)") in caplog.text


@pytest.mark.parametrize("argv", [["--target", "example-rds"],
                                  ["--database", "app"]])
def test_a_run_that_names_something_clears_nothing(refresh, run, argv):
    assert run(*argv) == 0
    assert refresh.refreshed == ["example-rds"], "the run itself still happened"
    assert refresh.cleared == []


def test_the_every_minute_clickhouse_run_clears_nothing(refresh, run, monkeypatch):
    ch = _target(engine="clickhouse", tid=5, alias="example-ch", host="ch.example.cloud")
    monkeypatch.setattr(refresh.targets_mod, "list_enabled", lambda: [ch])
    monkeypatch.setattr(refresh, "_clickhouse_states", lambda: {"ch.example.cloud": "running"})
    monkeypatch.setattr(refresh, "_attempted_within", lambda tid, hours: False)
    assert run("--clickhouse-when-fresh") == 0
    assert refresh.refreshed == ["example-ch"], "the run itself still happened"
    assert refresh.cleared == []


def test_a_failed_cleanup_does_not_stop_the_refresh(refresh, run, monkeypatch, caplog):
    def boom():
        raise RuntimeError("bot db blip")
    monkeypatch.setattr(refresh.schema_catalog, "clear_replica_catalog", boom)
    assert run() == 0
    assert refresh.refreshed == ["example-rds"]
    assert "replica catalog clean-up failed" in caplog.text


def test_the_report_lists_the_replicas_and_changes_nothing(refresh, run, monkeypatch, capsys):
    monkeypatch.setattr(refresh, "prune_report", lambda fleet: 0)
    assert run("--prune-report") == 0
    out = capsys.readouterr().out
    assert "example-replica: would remove 3 tables and 1 functions (read replica)" in out
    assert "1 read replica(s) would lose their catalog rows. Nothing was changed." in out
    assert refresh.cleared == [] and refresh.refreshed == []


def test_the_report_for_one_named_target_leaves_the_replicas_out(refresh, run, monkeypatch,
                                                                 capsys):
    monkeypatch.setattr(refresh, "prune_report", lambda fleet: 0)
    assert run("--prune-report", "--target", "example-rds") == 0
    assert "read replica" not in capsys.readouterr().out
