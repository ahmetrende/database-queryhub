"""Catalog refresh after a DDL request (schema_catalog.refresh_after_ddl).

A DDL request that completes starts one refresh of its own target and
database. RO and RW requests do not. The switch turns it off. A second
trigger for the same pair while one runs does nothing. A failure in the
refresh is logged and never reaches the run.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from queryhub import schema_catalog


def _target(**kw):
    base = dict(id=7, enabled=True, replica_of=None, engine="postgres")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture()
def env(monkeypatch):
    state = {"switch": True, "target": _target(), "password": "pw", "snaps": [],
             "snap_error": None}
    monkeypatch.setattr(schema_catalog.cfg, "get_bool",
                        lambda key, default: state["switch"]
                        if key == "catalog_refresh_after_ddl" else default)
    from queryhub import targets
    monkeypatch.setattr(targets, "get", lambda tid: state["target"])
    monkeypatch.setattr(schema_catalog, "_ro_password", lambda tid: state["password"])

    def snap(target, password, database):
        if state["snap_error"]:
            raise state["snap_error"]
        state["snaps"].append((target.id, password, database))
        return 3, 9
    monkeypatch.setattr(schema_catalog, "snapshot_database", snap)
    schema_catalog._refreshing.clear()
    yield state
    schema_catalog._refreshing.clear()


def inline(fn, name):
    fn()


def test_a_ddl_request_refreshes_its_own_database(env):
    assert schema_catalog.refresh_after_ddl(1, "ddl", 7, "shop", start=inline) is True
    assert env["snaps"] == [(7, "pw", "shop")]
    assert schema_catalog._refreshing == set()


@pytest.mark.parametrize("tier", ["ro", "rw", None, "set"])
def test_other_tiers_do_not_refresh(env, tier):
    assert schema_catalog.refresh_after_ddl(1, tier, 7, "shop", start=inline) is False
    assert env["snaps"] == []


def test_the_switch_turns_it_off(env):
    env["switch"] = False
    assert schema_catalog.refresh_after_ddl(1, "ddl", 7, "shop", start=inline) is False
    assert env["snaps"] == []


def test_missing_target_or_database_does_nothing(env):
    assert schema_catalog.refresh_after_ddl(1, "ddl", None, "shop", start=inline) is False
    assert schema_catalog.refresh_after_ddl(1, "ddl", 7, "", start=inline) is False
    assert env["snaps"] == []


def test_a_second_trigger_while_one_runs_does_nothing(env):
    held = []
    assert schema_catalog.refresh_after_ddl(1, "ddl", 7, "shop", start=lambda fn, n: held.append(fn)) is True
    assert schema_catalog.refresh_after_ddl(2, "ddl", 7, "shop", start=inline) is False
    # Another database of the same target is a different pair.
    assert schema_catalog.refresh_after_ddl(3, "ddl", 7, "other", start=inline) is True
    held[0]()
    assert env["snaps"] == [(7, "pw", "other"), (7, "pw", "shop")]
    assert schema_catalog._refreshing == set()


@pytest.mark.parametrize("target", [
    None, _target(enabled=False), _target(replica_of=3), _target(engine="clickhouse")])
def test_targets_the_hourly_run_skips_are_skipped(env, target):
    env["target"] = target
    assert schema_catalog.refresh_after_ddl(1, "ddl", 7, "shop", start=inline) is True
    assert env["snaps"] == []
    assert schema_catalog._refreshing == set()


def test_no_ro_password_skips_and_logs(env, caplog):
    env["password"] = None
    with caplog.at_level(logging.WARNING, logger=schema_catalog.log.name):
        schema_catalog.refresh_after_ddl(1, "ddl", 7, "shop", start=inline)
    assert env["snaps"] == []
    assert "no RO credential" in caplog.text


def test_a_failed_snapshot_is_logged_and_never_raised(env, caplog):
    env["snap_error"] = RuntimeError("password=secret host down")
    with caplog.at_level(logging.WARNING, logger=schema_catalog.log.name):
        assert schema_catalog.refresh_after_ddl(1, "ddl", 7, "shop", start=inline) is True
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text
    assert schema_catalog._refreshing == set()


def test_a_thread_that_cannot_start_releases_the_pair(env):
    def boom(fn, name):
        raise RuntimeError("no threads")
    assert schema_catalog.refresh_after_ddl(1, "ddl", 7, "shop", start=boom) is False
    assert schema_catalog._refreshing == set()


def test_every_completion_path_calls_the_refresh():
    from pathlib import Path
    src = (Path(schema_catalog.__file__).parent / "executor.py").read_text()
    assert src.count("_refresh_catalog_after_ddl(request)") == 5
    assert 'request["executed_tier"] = mode' in src
