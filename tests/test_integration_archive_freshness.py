"""Migration 140 against a real database: the column, the setting, and a
verdict's round trip.

The mocked tests pin how a marker becomes a verdict and how a verdict becomes a
sentence. What only a database can show is that the verdict comes back out of
jsonb exactly as it went in, that storing it leaves `updated_at` alone, and that
the hourly refresh finds a DISABLED archive by what its engine_config says.

The fixture is an Athena target no real deployment has, reused by alias so that
repeated runs do not accumulate rows. The refresh runs for that alias only: a
stray run against a live database must not write a fake verdict onto a real
archive.
"""
import importlib.util
import io
import json
import os
import pathlib

import pytest
from psycopg.types.json import Json

from queryhub import athena_exec, db, targets

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

ALIAS = "archive-freshness-fixture"
_MARKER = json.dumps({"covered_through": "2026-08-25T12:00:00Z",
                      "computed_at": "2026-09-30T11:46:37Z",
                      "known_gaps": [{"from": "2026-09-01T13:09:00Z",
                                      "to": "2026-09-01T13:10:00Z"}],
                      "note": "fixture"}).encode()


@pytest.fixture
def archive():
    row = db.fetch_one("SELECT id FROM target_servers WHERE alias = %s", (ALIAS,))
    if row:
        tid = row["id"]
    else:
        tid = db.insert_returning(
            "INSERT INTO target_servers (alias, host, port, default_database, engine, "
            "  enabled, engine_config) "
            "VALUES (%s, 'athena.example.test', 443, 'archive_db', "
            "  'athena', FALSE, %s) RETURNING id",
            (ALIAS, Json({"region": "eu-central-1", "workgroup": "wg",
                          "database": "archive_db",
                          "role_arn": "arn:aws:iam::111111111111:role/reader",
                          "freshness_marker":
                              "s3://example-archive/balance/_meta/watermark.json"})),
        )["id"]
    targets.set_archive_freshness(tid, None)
    yield tid
    targets.set_archive_freshness(tid, None)


def test_the_column_and_the_setting_exist():
    col = db.fetch_one(
        "SELECT data_type FROM information_schema.columns "
        " WHERE table_name = 'target_servers' AND column_name = 'archive_freshness'")
    assert col is not None and col["data_type"] == "jsonb"
    row = db.fetch_one(
        "SELECT value, description FROM bot_config WHERE key = 'athena_freshness_stale_hours'")
    assert row is not None and row["value"].isdigit() and row["description"]


def test_a_verdict_comes_back_as_it_went_in_and_leaves_updated_at_alone(archive):
    verdict = {**athena_exec.parse_marker(_MARKER), "read_at": "2026-09-30T12:00:00Z"}
    before = db.fetch_one("SELECT updated_at FROM target_servers WHERE id = %s",
                          (archive,))["updated_at"]
    targets.set_archive_freshness(archive, verdict)
    assert targets.archive_freshness(archive) == verdict
    after = db.fetch_one("SELECT updated_at FROM target_servers WHERE id = %s",
                         (archive,))["updated_at"]
    assert after == before
    targets.set_archive_freshness(archive, None)
    assert targets.archive_freshness(archive) is None


def test_the_refresh_finds_a_disabled_archive_by_its_marker(archive, monkeypatch):
    class _Client:
        def get_object(self, Bucket, Key):
            assert (Bucket, Key) == ("example-archive", "balance/_meta/watermark.json")
            return {"Body": io.BytesIO(_MARKER)}

    class _Session:
        def client(self, name, region_name=None):
            return _Client()
    monkeypatch.setattr(athena_exec, "session", lambda cfg: _Session())

    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "refresh_schema_catalog.py"
    spec = importlib.util.spec_from_file_location("refresh_schema_catalog_it", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.refresh_archive_freshness(only_alias=ALIAS) == 0
    stored = targets.archive_freshness(archive)
    assert stored["state"] == "complete_with_gaps"
    assert stored["known_gaps"] == [{"from": "2026-09-01T13:09:00Z",
                                     "to": "2026-09-01T13:10:00Z"}]
    assert athena_exec.freshness_sentence(stored, stale_hours=10_000).startswith(
        "Archive complete up to 25 Aug 2026 12:00 UTC. Known gaps: ")
