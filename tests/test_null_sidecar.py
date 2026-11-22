"""NULL sidecar: the writer keeps `<name>.csv.nulls` and the JSON and SQL
exports use it to keep NULL and an empty string apart."""
from __future__ import annotations

import importlib.util
import os
from datetime import datetime
from pathlib import Path

import pytest

from queryhub import executor, result_export
from queryhub.result_export import null_mask, stream_json, stream_sql_insert


class _FakeCur:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


@pytest.fixture(autouse=True)
def _tmp_csv_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "CSV_DIR", tmp_path)


def _side(p: Path) -> Path:
    return p.with_name(p.name + ".nulls")


def _text(chunks) -> str:
    return b"".join(chunks).decode("utf-8")


def _write(rows, cols=("a", "b"), cap=10_000_000):
    path, n, _tr, ts = executor._stream_to_csv(
        7, list(cols), _FakeCur(rows), max_rows=1000, max_csv_bytes=cap)
    return path, n, ts


# ---- writer -----------------------------------------------------------------

def test_writer_keeps_sidecar_and_same_csv_bytes(tmp_path, monkeypatch):
    rows = [("x", None), ("", "y"), (None, None)]
    path, n, ts = _write(rows)
    assert (n, ts) == (3, False)
    assert path.read_bytes() == b"a,b\r\nx,\r\n,y\r\n,\r\n"
    assert _side(path).read_text() == "v1 2\n0 1\n2 0,1\n"
    assert not list(tmp_path.glob("*.tmp"))
    assert oct(_side(path).stat().st_mode & 0o777) == "0o600"


def test_writer_skips_sidecar_without_nulls():
    path, *_ = _write([("x", ""), ("", "y")])
    assert path.is_file()
    assert not _side(path).exists()


def test_writer_cap_drops_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "_NULLS_CAP_BYTES", 10)
    path, n, _ = _write([(None, None)] * 50)
    assert n == 50
    assert not _side(path).exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_writer_no_sidecar_when_size_truncated(tmp_path):
    path, _n, ts = _write([(None, "x" * 50)] * 10, cap=200)
    assert ts is True
    assert not _side(path).exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_move_to_result_set_1_carries_sidecar(tmp_path):
    path = tmp_path / "req_7_q1_20260101T000000Z.csv"
    path.write_text("a\r\n\r\n")
    _side(path).write_text("v1 1\n0 0\n")
    first = executor._StmtResult(index=1, leading="UPDATE", csv_path=path,
                                 has_result_set=True)
    moved = executor._move_to_result_set_1(first)
    assert moved.csv_path.name == "req_7_q1s1_20260101T000000Z.csv"
    assert _side(moved.csv_path).read_text() == "v1 1\n0 0\n"
    assert not _side(path).exists()


# ---- reader -----------------------------------------------------------------

def test_null_mask_reads_sidecar():
    path, *_ = _write([("x", None), ("", "y"), (None, None)])
    assert null_mask(path) == {0: {1}, 2: {0, 1}}


@pytest.mark.parametrize("body", [
    "", "v2 2\n", "v1 3\n0 1\n", "v1 2\n0 5\n", "v1 2\nzero 1\n",
    "v1 2\n0 1,x\n", "v1 2\n0\n", "garbage",
])
def test_null_mask_malformed_is_none(tmp_path, body):
    path = tmp_path / "r.csv"
    path.write_text("a,b\r\n,\r\n")
    _side(path).write_text(body)
    assert null_mask(path) is None


def test_null_mask_missing_is_none(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text("a,b\r\n,\r\n")
    assert null_mask(path) is None


# ---- exports ----------------------------------------------------------------

def test_sql_with_mask_keeps_empty_string_apart():
    out = _text(stream_sql_insert(["a", "b"], [("", ""), ("it's", "")],
                                  nulls={0: {1}, 1: {1}}))
    assert "NULL and empty strings are kept apart" in out
    assert "VALUES ('', NULL);" in out
    assert "VALUES ('it''s', NULL);" in out


def test_sql_without_mask_is_unchanged():
    rows = [("", "x")]
    out = _text(stream_sql_insert(["a", "b"], rows))
    assert out.startswith(result_export._SQL_HEADER)
    assert "VALUES (NULL, 'x');" in out


def test_json_with_mask_keeps_empty_string_apart():
    types = {"a": "integer", "b": "text"}
    out = _text(stream_json(["a", "b"], [("", ""), ("5", "")], types,
                            nulls={0: {1}, 1: {1}}))
    assert '{"a":"","b":null}' in out
    assert '{"a":5,"b":null}' in out


def test_json_without_mask_is_unchanged():
    types = {"a": "integer", "b": "text"}
    out = _text(stream_json(["a", "b"], [("", "")], types))
    assert '{"a":null,"b":""}' in out


def test_malformed_sidecar_falls_back(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text("a,b\r\n,\r\n")
    _side(path).write_text("v1 9\n0 0\n")
    nulls = null_mask(path)
    assert nulls is None
    out = _text(stream_sql_insert(["a", "b"], [("", "")], nulls=nulls))
    assert "VALUES (NULL, NULL);" in out


# ---- cleanup ----------------------------------------------------------------

def _cleanup_mod(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parent.parent / "scripts" / "cleanup_old_results.py"
    spec = importlib.util.spec_from_file_location("cleanup_old_results", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "RESULTS_DIR", tmp_path)
    return mod


def test_cleanup_removes_sidecar_with_csv(tmp_path, monkeypatch):
    mod = _cleanup_mod(monkeypatch, tmp_path)
    path, *_ = _write([(None, "x")])
    assert _side(path).is_file()
    old = datetime.now().timestamp() - 10 * 3600
    os.utime(path, (old, old))
    mod.cleanup_local(1)
    assert not path.exists()
    assert not _side(path).exists()


def test_cleanup_removes_orphan_and_stale_tmp(tmp_path, monkeypatch):
    mod = _cleanup_mod(monkeypatch, tmp_path)
    orphan = tmp_path / "gone.csv.nulls"
    orphan.write_text("v1 1\n0 0\n")
    tmp = tmp_path / "gone.csv.nulls.tmp"
    tmp.write_text("v1 1\n")
    old = datetime.now().timestamp() - 10 * 3600
    os.utime(tmp, (old, old))
    mod.cleanup_local(1)
    assert not orphan.exists()
    assert not tmp.exists()
