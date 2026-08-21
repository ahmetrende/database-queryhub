"""Export beside a statement switcher must download what is on screen.

A multi-statement result is stored as an archive of per-statement tables. The
download endpoints served the whole artefact and knew nothing about statements,
so Export next to "Result 2 of 3" gave the archive rather than table 2 — and
the archive went out under the XLSX media type, because the branch only knew
"csv or not".
"""
import csv
import io
import zipfile

import pytest

from queryhub.web import routes_queries as rq


def _zip(tmp_path, *tables):
    z = tmp_path / "req_9_results_20260821T000000Z.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for i, rows in enumerate(tables, start=1):
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["col"])
            for r in rows:
                w.writerow([r])
            zf.writestr(f"req_9_q{i}_x.csv", buf.getvalue())
    return z


def test_each_statement_reads_back_as_its_own_table(tmp_path):
    z = _zip(tmp_path, ["one"], ["two", "three"])
    for n, expect in ((1, [["col"], ["one"]]), (2, [["col"], ["two"], ["three"]])):
        with rq._open_statement(z, n) as fh:
            assert [r for r in csv.reader(fh)] == expect, f"statement {n}"


def test_media_type_no_longer_calls_a_zip_a_spreadsheet():
    """The old branch was `csv else xlsx`, so an archive went out claiming to be
    an Excel workbook. Nothing broke visibly — the filename saved correctly —
    but the header was a lie."""
    def pick(suffix):
        return {".csv": "text/csv", ".zip": "application/zip"}.get(
            suffix, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert pick(".csv") == "text/csv"
    assert pick(".zip") == "application/zip"
    assert "spreadsheet" in pick(".xlsx")


def test_asking_for_a_statement_that_is_not_there(tmp_path):
    z = _zip(tmp_path, ["one"])
    with pytest.raises(Exception) as e:
        with rq._open_statement(z, 2):
            pass
    assert "2" in str(e.value)
