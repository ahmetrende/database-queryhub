"""Unit tests for the migration runner's pure planning logic (no DB)."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "apply_migrations",
    Path(__file__).resolve().parent.parent / "scripts" / "apply_migrations.py",
)
apply_migrations = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(apply_migrations)  # type: ignore[union-attr]

plan = apply_migrations.plan
_checksum = apply_migrations._checksum


def test_new_migration_is_applied():
    entries = [("073_x.sql", "aaa")]
    assert plan(entries, {}) == [("apply", "073_x.sql")]


def test_recorded_matching_checksum_is_skipped():
    entries = [("073_x.sql", "aaa")]
    assert plan(entries, {"073_x.sql": "aaa"}) == [("skip", "073_x.sql")]


def test_recorded_changed_checksum_is_dirty():
    entries = [("073_x.sql", "bbb")]
    assert plan(entries, {"073_x.sql": "aaa"}) == [("dirty", "073_x.sql")]


def test_mixed_batch_preserves_order_and_classification():
    entries = [
        ("070_a.sql", "h70"),   # already applied, unchanged
        ("071_b.sql", "h71x"),  # applied but edited -> dirty
        ("072_c.sql", "h72"),   # new
    ]
    applied = {"070_a.sql": "h70", "071_b.sql": "h71"}
    assert plan(entries, applied) == [
        ("skip", "070_a.sql"),
        ("dirty", "071_b.sql"),
        ("apply", "072_c.sql"),
    ]


def test_checksum_is_stable_and_content_sensitive():
    assert _checksum("SELECT 1;") == _checksum("SELECT 1;")
    assert _checksum("SELECT 1;") != _checksum("SELECT 2;")
