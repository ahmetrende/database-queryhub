"""executor export safety — formula injection, XLSX streaming past 256 rows.

These exercise the CSV/XLSX writers directly with a fake cursor and a temp
output dir (CSV_DIR patched), so no DB or Slack is needed.
"""
import csv as csvmod

import pytest

from queryhub import executor


class _FakeCur:
    """Minimal cursor: iterating yields the given rows."""
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


@pytest.fixture(autouse=True)
def _tmp_csv_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "CSV_DIR", tmp_path)


# ---- formula injection ------------------------------------------------------

def test_neutralize_formula_guards_risky_prefixes():
    for bad in ("=1+1", "+cmd", "-2+3", "@ref", "\ttab", "\rcr"):
        assert executor._neutralize_formula(bad) == "'" + bad
    # safe strings + non-strings pass through untouched
    assert executor._neutralize_formula("hello") == "hello"
    assert executor._neutralize_formula("a=b") == "a=b"
    assert executor._neutralize_formula(42) == 42
    assert executor._neutralize_formula(None) is None


def test_csv_export_neutralizes_formulas(tmp_path):
    cur = _FakeCur([("=SUM(A1:A9)", "ok"), ("+bad", "-also")])
    path, n, tr, ts = executor._stream_to_csv(
        1, ["a", "b"], cur, max_rows=100, max_csv_bytes=10_000_000)
    assert (n, tr, ts) == (2, False, False)
    rows = list(csvmod.reader(path.open()))
    assert rows[0] == ["a", "b"]
    assert rows[1][0] == "'=SUM(A1:A9)"   # neutralized
    assert rows[2][0] == "'+bad" and rows[2][1] == "'-also"


# ---- XLSX streaming (regression: used to break past 256 rows) --------------

@pytest.mark.parametrize("count", [255, 256, 257, 1000])
def test_xlsx_export_handles_large_results(count):
    openpyxl = pytest.importorskip("openpyxl")
    cur = _FakeCur([(i, "x") for i in range(count)])
    path, n, tr, ts = executor._stream_to_xlsx(
        7, ["n", "v"], cur, max_rows=100_000, max_csv_bytes=50_000_000)
    assert (n, tr, ts) == (count, False, False)
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb["result"]
    got = list(ws.iter_rows(values_only=True))
    assert got[0] == ("n", "v")
    assert len(got) == count + 1          # header + data
    assert got[-1] == (count - 1, "x")
    wb.close()


def test_xlsx_export_neutralizes_formula_cells():
    openpyxl = pytest.importorskip("openpyxl")
    cur = _FakeCur([("=1+1", "ok")])
    path, n, _, _ = executor._stream_to_xlsx(
        8, ["a", "b"], cur, max_rows=10, max_csv_bytes=10_000_000)
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb["result"]
    val = list(ws.iter_rows(values_only=True))[1][0]
    assert val == "'=1+1"                 # stored as literal text, not a formula
    wb.close()


# ---- the header row is guarded too (2026-09-24) -----------------------------
# A column name is text the requester chooses (`AS "=HYPERLINK(...)"`), and the
# header row went out unguarded on every path while each data cell was guarded.

def test_csv_export_neutralizes_the_header():
    path, *_ = executor._stream_to_csv(
        2, ["=1+1", "ok"], _FakeCur([(1, 2)]), max_rows=10, max_csv_bytes=10_000_000)
    assert list(csvmod.reader(path.open()))[0] == ["'=1+1", "ok"]


def test_xlsx_export_writes_the_header_as_text_not_a_formula():
    openpyxl = pytest.importorskip("openpyxl")
    path, *_ = executor._stream_to_xlsx(
        3, ["=1+1", "ok"], _FakeCur([(1, 2)]), max_rows=10, max_csv_bytes=10_000_000)
    wb = openpyxl.load_workbook(path)          # not read-only: data_type is needed
    cell = wb["result"]["A1"]
    assert cell.value == "'=1+1" and cell.data_type != "f"
    wb.close()


def test_the_web_xlsx_conversion_guards_every_cell_including_the_header():
    """The web converts a stored CSV to XLSX on download. A CSV written before
    this fix still carries a raw header, so the conversion guards it too, and
    guarding an already-guarded cell changes nothing."""
    import inspect
    from queryhub.web import routes_queries
    src = inspect.getsource(routes_queries.query_result_xlsx)
    assert "ws.append([_xlsx_cell(v) for v in rec])" in src
    assert "ws.append(rec)" not in src
    assert executor._xlsx_cell(executor._xlsx_cell("=1+1")) == "'=1+1"
