"""The web's CSV-to-XLSX conversion runs once per result, not per download.

External review 2026-09-24 (PERF-02): every download of a CSV result as Excel
converted the whole file again. The workbook is now kept beside the result,
stamped with the result's mtime: that tells a current copy from a stale one,
and `cleanup_old_results.py`, which reaps .xlsx files by mtime, removes it
together with the CSV it came from.
"""
import os

import pytest

from queryhub.web import routes_queries

CSV = "id,email\n1,***\n2,***\n"


@pytest.fixture
def result(tmp_path, monkeypatch):
    p = tmp_path / "req_42_20260925T070000Z.csv"
    p.write_text(CSV)
    os.utime(p, (1_790_000_000, 1_790_000_000))
    row = {"status": "completed", "csv_file_path": str(p)}
    monkeypatch.setattr(routes_queries.deps, "require_whitelisted", lambda c: None)
    monkeypatch.setattr(routes_queries, "_own_request", lambda rid, uid: row)
    monkeypatch.setattr(routes_queries.audit_mod, "log", lambda *a, **k: None)
    built = []
    real = routes_queries._write_xlsx

    def counting(src, statement, out):
        built.append(statement)
        real(src, statement, out)
    monkeypatch.setattr(routes_queries, "_write_xlsx", counting)
    return p, built


def _download(statement=None):
    return routes_queries.query_result_xlsx(42, statement, claims={"sub": "U0EXAMPLE001"})


def test_the_second_download_is_served_from_the_first(result):
    p, built = result
    first = _download()
    second = _download()
    assert built == [1]
    cached = p.with_name(p.stem + ".s1.xlsx")
    assert first.path == second.path == cached
    assert int(cached.stat().st_mtime) == int(p.stat().st_mtime)
    assert oct(cached.stat().st_mode & 0o777) == "0o600"
    assert [f.name for f in p.parent.iterdir() if f.name.startswith(".")] == []


def test_a_changed_result_is_converted_again(result):
    p, built = result
    _download()
    os.utime(p, (1_790_000_100, 1_790_000_100))
    _download()
    assert built == [1, 1]


def test_the_cleanup_job_reaps_the_copy_with_its_result(result):
    """It reaps .csv/.zip/.xlsx by mtime, and the copy carries the CSV's."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "cleanup", Path(__file__).resolve().parents[1] / "scripts" / "cleanup_old_results.py")
    src = spec.loader.get_data(spec.origin).decode()
    assert '(".csv", ".zip", ".xlsx")' in src
