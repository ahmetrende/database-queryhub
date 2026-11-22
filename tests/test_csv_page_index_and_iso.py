"""Deep-page sidecar index for stored CSV results, and iso() in UTC."""
from __future__ import annotations

import csv
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from queryhub.web import mapping, routes_queries as rq


def _write_csv(path: Path, n: int) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "note", "dup", "dup"])
        for i in range(n):
            note = f"line one {i}\nline \"two\", {i}" if i % 3 == 0 else f"n{i}"
            w.writerow([i, note, "", ""])


def _full_scan(path: Path, offset: int, limit: int):
    with path.open(newline="", encoding="utf-8") as fh:
        cols: list[str] = []
        out: list[dict] = []
        for i, rec in enumerate(csv.reader(fh)):
            if i == 0:
                cols = rq._dedup_cols(rec)
                continue
            if (i - 1) < offset:
                continue
            if len(out) >= limit:
                break
            out.append(dict(zip(cols, rec)))
    return cols, out


def test_indexed_page_equals_full_scan(tmp_path):
    p = tmp_path / "r.csv"
    _write_csv(p, 5500)
    for offset in (2000, 2001, 2999, 3000, 4321, 5400):
        got = rq._page_from_index(p, offset, 150)
        assert got == _full_scan(p, offset, 150), offset
    assert rq._row_index_path(p).is_file()


def test_stale_index_is_rebuilt(tmp_path):
    p = tmp_path / "r.csv"
    _write_csv(p, 3000)
    assert rq._page_from_index(p, 2500, 10) is not None
    _write_csv(p, 4500)
    assert rq._page_from_index(p, 4200, 10) == _full_scan(p, 4200, 10)


def test_offset_past_end_defers_to_full_scan(tmp_path):
    p = tmp_path / "r.csv"
    _write_csv(p, 2500)
    assert rq._page_from_index(p, 9000, 10) is None


def test_cleanup_removes_index_with_csv(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parent.parent / "scripts" / "cleanup_old_results.py"
    spec = importlib.util.spec_from_file_location("cleanup_old_results", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "RESULTS_DIR", tmp_path)
    p = tmp_path / "r.csv"
    _write_csv(p, 3000)
    rq._page_from_index(p, 2500, 10)
    side = rq._row_index_path(p)
    assert side.is_file()
    old = datetime.now().timestamp() - 10 * 3600
    os.utime(p, (old, old))
    mod.cleanup_local(1)
    assert not p.exists()
    assert not side.exists()


def test_iso_aware_value_is_utc():
    tz = timezone(timedelta(hours=3))
    got = mapping.iso(datetime(2026, 1, 1, 12, 0, tzinfo=tz))
    assert got == "2026-01-01T09:00:00+00:00"


def test_iso_naive_and_none_unchanged():
    assert mapping.iso(datetime(2026, 1, 1, 12, 0)) == "2026-01-01T12:00:00"
    assert mapping.iso(None) is None
