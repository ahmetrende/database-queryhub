"""A streamed Postgres read gets the same cell format as any other read.

`_execute_main_statement` streams a Postgres SELECT with `cur.stream()` and
hands the result writer the ROW ITERATOR, not the cursor. The writer read the
column types from `getattr(cur, "description", None)`, and an iterator has no
description. So no column got its format on the most common path. Measured
2026-11-14 after PR 361: a bytea still reached the web grid as `b'\\x89PNG...'`.
A `timestamp(3)` lost its trim on the same path.
"""
import csv
from datetime import datetime

import pytest

from queryhub import executor as ex

PNG = bytes.fromhex("89504e47")
AT = datetime(2026, 7, 30, 1, 2, 3, 400000)


class _Col:
    """psycopg3's Column: indexable and attribute-bearing."""

    def __init__(self, name, type_code, precision=None):
        self.name = name
        self.type_code = type_code
        self.precision = precision
        self.type_display = "x"
        self.null_ok = None

    def __getitem__(self, i):
        return (self.name, self.type_code, None, None, self.precision, None,
                self.null_ok)[i]

    def __len__(self):
        return 7


class _StreamCur:
    """Mimics psycopg for a streamed SELECT: bytea and timestamp(3)."""
    rowcount = -1

    def __init__(self):
        self.description = [_Col("img", 17), _Col("at", 1114, precision=3)]

    def stream(self, sql):
        return iter([(PNG, AT)])

    def execute(self, sql, prepare=False):   # pragma: no cover - not used
        raise AssertionError("a SELECT must stream")


def _run(tmp_path, monkeypatch, result_format="csv"):
    monkeypatch.setattr(ex, "CSV_DIR", tmp_path)
    monkeypatch.setattr(ex.stmt_guard, "check", lambda *a, **k: None)
    monkeypatch.setattr(ex.pii, "is_enabled", lambda: False)
    stmt = type("S", (), {"rewritten": "SELECT img, at FROM t", "leading": "SELECT"})()
    return ex._execute_main_statement(
        _StreamCur(), stmt, 1, 14142, True, 100, 1_000_000,
        result_format=result_format, force_extended=True)


def test_a_streamed_csv_has_hex_bytea_and_trimmed_time(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch)
    with res.csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows == [["img", "at"], ["\\x89504e47", "2026-07-30 01:02:03.400"]]


def test_a_streamed_xlsx_has_hex_bytea(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    res = _run(tmp_path, monkeypatch, result_format="xlsx")
    wb = openpyxl.load_workbook(res.csv_path, read_only=True)
    got = list(wb["result"].iter_rows(values_only=True))
    wb.close()
    assert got[1][0] == "\\x89504e47"
