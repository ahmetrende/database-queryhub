"""Where an archive's freshness verdict is stored, and where it is read.

The hourly catalog refresh reads each Athena archive's freshness marker and
stores the verdict on the target row (migration 140); the submit path reads the
stored verdict and hands it to the approver's hint. The parsing and the sentence
are pinned in test_athena_freshness.py. This file pins the plumbing around them:

* the refresh reads every archive that names a marker, DISABLED ones included
  (an archive is enabled once it is ready, and its first approver should not
  see "unknown" for an hour), and one broken archive never stops the others;
* the stored verdict reaches the hint, and a marker that is named but has no
  verdict stored reads as unknown rather than as nothing.
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import pathlib

import pytest

from queryhub import athena_exec, lifecycle, targets
from queryhub import core_submit as cs

ROOT = pathlib.Path(__file__).resolve().parent.parent
_MARKER = "s3://example-archive/balance/_meta/ledgers-watermark.json"
_ENGINE_CONFIG = {"region": "eu-central-1", "workgroup": "wg", "database": "archive_db",
                  "role_arn": "arn:aws:iam::111111111111:role/reader"}


def _target(tid, alias, *, engine="athena", enabled=False, marker=_MARKER, **config):
    engine_config = {**_ENGINE_CONFIG, **config}
    if marker:
        engine_config["freshness_marker"] = marker
    return targets.TargetServer(
        id=tid, alias=alias, host="athena.example.test", port=443,
        default_database="archive_db", username=None, enabled=enabled, notes=None,
        engine=engine, engine_config=engine_config)


# ---------------------------------------------------------------------------
# the column
# ---------------------------------------------------------------------------

def test_the_stored_verdict_is_read_back_as_a_dict(monkeypatch):
    verdict = {"state": "complete", "covered_through": "2026-08-25T12:00:00Z"}
    rows = {7: {"archive_freshness": verdict}, 8: {"archive_freshness": None},
            9: {"archive_freshness": "complete"}}
    monkeypatch.setattr(targets.db, "fetch_one", lambda sql, p: rows.get(p[0]))
    assert targets.archive_freshness(7) == verdict
    assert targets.archive_freshness(8) is None           # not read yet
    assert targets.archive_freshness(9) is None           # not a verdict
    assert targets.archive_freshness(404) is None         # no such target


def test_storing_a_verdict_writes_that_column_and_nothing_else(monkeypatch):
    """Not updated_at: that says a person changed the connection."""
    seen = []
    monkeypatch.setattr(targets.db, "execute", lambda sql, params: seen.append((sql, params)))
    verdict = {"state": "unknown", "reason": "no marker", "read_at": "2026-09-30T12:00:00Z"}
    targets.set_archive_freshness(7, verdict)
    targets.set_archive_freshness(8, None)
    (sql, params), (_sql2, params2) = seen
    assert sql == "UPDATE target_servers SET archive_freshness = %s WHERE id = %s"
    assert "updated_at" not in sql
    assert params[0].obj == verdict and params[1] == 7      # sent as jsonb
    assert params2 == (None, 8)


# ---------------------------------------------------------------------------
# the hourly refresh
# ---------------------------------------------------------------------------

@pytest.fixture
def refresh(monkeypatch):
    path = ROOT / "scripts" / "refresh_schema_catalog.py"
    spec = importlib.util.spec_from_file_location("refresh_schema_catalog_fresh", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    stored: dict[int, dict] = {}
    monkeypatch.setattr(mod.targets_mod, "set_archive_freshness",
                        lambda tid, verdict: stored.__setitem__(tid, verdict))
    return mod, stored


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _s3(monkeypatch, objects: dict):
    """A session whose S3 serves `objects` by key: bytes, or an exception."""
    class _Client:
        def get_object(self, Bucket, Key):
            outcome = objects[Key]
            if isinstance(outcome, BaseException):
                raise outcome
            return {"Body": io.BytesIO(outcome)}

    class _Session:
        def client(self, name, region_name=None):
            assert name == "s3"
            return _Client()
    monkeypatch.setattr(athena_exec, "session", lambda cfg: _Session())


_GOOD = json.dumps({"covered_through": "2026-08-25T12:00:00Z",
                    "computed_at": "2026-09-30T11:46:37Z", "note": "interim"}).encode()


def test_every_archive_that_names_a_marker_is_read_enabled_or_not(refresh, monkeypatch):
    mod, stored = refresh
    fleet = [
        _target(1, "archive-live", enabled=True, marker="s3://b/live.json"),
        _target(2, "archive-parked", enabled=False, marker="s3://b/parked.json"),
        _target(3, "archive-without-marker", marker=None),
        _target(4, "pg-with-a-stray-key", engine="postgres", marker="s3://b/stray.json"),
    ]
    monkeypatch.setattr(mod.targets_mod, "list_all", lambda: fleet)
    _s3(monkeypatch, {"live.json": _GOOD, "parked.json": _GOOD})

    assert mod.refresh_archive_freshness() == 0
    assert sorted(stored) == [1, 2]
    assert stored[2]["state"] == "complete"
    assert stored[2]["covered_through"] == "2026-08-25T12:00:00Z"
    assert "read_at" in stored[2]


def test_an_s3_error_is_stored_as_unknown_and_the_rest_are_still_read(refresh, monkeypatch,
                                                                     caplog):
    mod, stored = refresh
    fleet = [_target(1, "archive-denied", marker="s3://b/denied.json"),
             _target(2, "archive-missing", marker="s3://b/missing.json"),
             _target(3, "archive-good", marker="s3://b/good.json")]
    monkeypatch.setattr(mod.targets_mod, "list_all", lambda: fleet)
    _s3(monkeypatch, {"denied.json": _ClientError("AccessDenied"),
                      "missing.json": _ClientError("NoSuchKey"), "good.json": _GOOD})

    with caplog.at_level(logging.INFO, logger="refresh_schema_catalog"):
        assert mod.refresh_archive_freshness() == 0     # an answer, not a failure
    assert (stored[1]["state"], stored[1]["reason"]) == ("unknown", "unreadable")
    assert (stored[2]["state"], stored[2]["reason"]) == ("unknown", "no marker")
    assert stored[3]["state"] == "complete"
    lines = {r.getMessage().split(":")[0]: r.levelno for r in caplog.records
             if "/freshness" in r.getMessage()}
    assert lines == {"archive-denied/freshness": logging.WARNING,
                     "archive-missing/freshness": logging.WARNING,
                     "archive-good/freshness": logging.INFO}


def test_a_target_that_cannot_be_refreshed_is_counted_and_skipped(refresh, monkeypatch):
    """A half-filled engine_config, then a write that fails: each is one
    failure, and the archive after them is still read and stored."""
    mod, stored = refresh
    fleet = [_target(1, "archive-no-region", marker="s3://b/good.json", region=""),
             _target(2, "archive-write-fails", marker="s3://b/good.json"),
             _target(3, "archive-good", marker="s3://b/good.json")]
    monkeypatch.setattr(mod.targets_mod, "list_all", lambda: fleet)
    _s3(monkeypatch, {"good.json": _GOOD})

    def store(tid, verdict):
        if tid == 2:
            raise RuntimeError("the metadata DB went away")
        stored[tid] = verdict
    monkeypatch.setattr(mod.targets_mod, "set_archive_freshness", store)

    assert mod.refresh_archive_freshness() == 2
    assert sorted(stored) == [3]


def test_a_listing_that_fails_does_not_raise(refresh, monkeypatch):
    mod, _stored = refresh

    def boom():
        raise RuntimeError("pool exhausted")
    monkeypatch.setattr(mod.targets_mod, "list_all", boom)
    assert mod.refresh_archive_freshness() == 1


def test_only_alias_limits_the_pass(refresh, monkeypatch):
    mod, stored = refresh
    fleet = [_target(1, "archive-a", marker="s3://b/good.json"),
             _target(2, "archive-b", marker="s3://b/good.json")]
    monkeypatch.setattr(mod.targets_mod, "list_all", lambda: fleet)
    _s3(monkeypatch, {"good.json": _GOOD})
    mod.refresh_archive_freshness(only_alias="archive-b")
    assert sorted(stored) == [2]


@pytest.fixture
def main_run(refresh, monkeypatch):
    """main() with the catalog half stubbed out, recording the freshness pass."""
    mod, _stored = refresh
    calls = []
    monkeypatch.setattr(mod, "refresh_archive_freshness",
                        lambda only_alias=None: calls.append(only_alias) or 0)
    fleet = [_target(1, "archive-live", enabled=True)]
    monkeypatch.setattr(mod.targets_mod, "list_enabled", lambda: fleet)
    monkeypatch.setattr(mod, "refresh_target", lambda t, only_database=None: {})
    monkeypatch.setattr(mod.schema_catalog, "summary_is_ok", lambda s: (True, None))
    monkeypatch.setattr(mod.schema_catalog, "record_refresh",
                        lambda *a: {"alert": False, "recovered": False})
    monkeypatch.setattr(mod, "_announce", lambda n: None)

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["refresh", *argv])
        assert mod.main() == 0
        return calls
    return run


def test_the_hourly_run_reads_the_markers(main_run):
    assert main_run() == [None]


def test_a_named_target_reads_only_its_own_marker(main_run):
    assert main_run("--target", "archive-live") == ["archive-live"]


def test_a_database_run_reads_no_marker(main_run):
    assert main_run("--database", "archive_db") == []


def test_the_every_minute_clickhouse_run_reads_no_marker(main_run, refresh, monkeypatch):
    mod, _stored = refresh
    ch = targets.TargetServer(id=5, alias="ch", host="ch.example.cloud", port=9440,
                              default_database="default", username="u", enabled=True,
                              notes=None, engine="clickhouse")
    monkeypatch.setattr(mod.targets_mod, "list_enabled", lambda: [ch])
    monkeypatch.setattr(mod, "_clickhouse_states", lambda: {"ch.example.cloud": "running"})
    monkeypatch.setattr(mod, "_attempted_within", lambda tid, hours: False)
    assert main_run("--clickhouse-when-fresh") == []


# ---------------------------------------------------------------------------
# the submit path
# ---------------------------------------------------------------------------

@pytest.fixture
def submit(monkeypatch):
    """validate_submission for an Athena target, with the hint recorded
    instead of computed. `go()` returns (Prepared, the freshness it was given)."""
    state = {"target": _target(9, "archive", enabled=True)}
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(cs.targets, "get", lambda tid: state["target"])
    monkeypatch.setattr(cs.teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ro"})
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "ro")
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(cs.auto_approve, "effective_grant", lambda *a, **k: None)
    monkeypatch.setattr(cs.db, "fetch_one", lambda sql, params=None: None)

    seen = {}

    def hint(cfg, sql, *, database=None, freshness=None):
        seen["freshness"] = freshness
        return "the hint"
    monkeypatch.setattr(athena_exec, "risk_hint", hint)

    def go(target=None):
        if target is not None:
            state["target"] = target
        out = cs.validate_submission(
            "U0EXAMPLE002", "Ex", target_server_id=9, database_name=None,
            query="SELECT count(*) FROM ledgers", justification="because")
        assert isinstance(out, cs.Prepared), out
        return out, seen.get("freshness", "not called")
    return go


def test_the_stored_verdict_reaches_the_hint(submit, monkeypatch):
    verdict = {"state": "complete", "covered_through": "2026-08-25T12:00:00Z"}
    monkeypatch.setattr(cs.targets, "archive_freshness",
                        lambda tid: verdict if tid == 9 else None)
    prep, freshness = submit()
    assert freshness == verdict
    assert prep.risk_summary == "the hint"


def test_a_named_marker_with_nothing_stored_is_unknown_not_silence(submit, monkeypatch):
    monkeypatch.setattr(cs.targets, "archive_freshness", lambda tid: None)
    _prep, freshness = submit()
    assert freshness["state"] == "unknown" and freshness["reason"] == "not read yet"


def test_a_stored_verdict_that_cannot_be_read_is_unknown(submit, monkeypatch):
    def boom(tid):
        raise RuntimeError('column "archive_freshness" does not exist')
    monkeypatch.setattr(cs.targets, "archive_freshness", boom)
    prep, freshness = submit()
    assert freshness["state"] == "unknown"
    assert prep.risk_summary == "the hint"               # the cost half is not lost


def test_an_archive_that_names_no_marker_gets_no_sentence(submit, monkeypatch):
    def never(tid):
        raise AssertionError("nothing to look up without a marker")
    monkeypatch.setattr(cs.targets, "archive_freshness", never)
    _prep, freshness = submit(_target(9, "archive", enabled=True, marker=None))
    assert freshness is None
