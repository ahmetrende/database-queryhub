"""A multi-statement result has to be readable in the grid.

The executor stores one plain CSV for a single statement and a zip of
per-statement CSVs for several. Both grid endpoints used to refuse anything
that was not a plain `.csv`, so a request with two SELECTs answered 409 —
and the UI, with nothing to render, stayed on the Messages tab while the
header reported the rows the query had returned. Requests 4481 and 4536 were
both this.
"""
import csv
import zipfile

import pytest

from queryhub.web.routes_queries import _open_statement, _statement_members


def _csv(path, header, *rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def _zip_of(tmp_path, *names):
    """A results zip whose members are named like the executor names them."""
    z = tmp_path / "req_1_results_20260819T000000Z.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for i, n in enumerate(names, start=1):
            member = tmp_path / n
            _csv(member, ["col"], [f"stmt{i}"])
            zf.write(member, arcname=n)
    return z


def test_plain_csv_is_a_single_statement(tmp_path):
    p = _csv(tmp_path / "req_1_q1_x.csv", ["a"], ["1"])
    assert _statement_members(p) == [p.name]


def test_zip_members_are_listed_in_statement_order(tmp_path):
    z = _zip_of(tmp_path, "req_1_q1_x.csv", "req_1_q2_x.csv", "req_1_q3_x.csv")
    assert _statement_members(z) == [
        "req_1_q1_x.csv", "req_1_q2_x.csv", "req_1_q3_x.csv"]


def test_statement_ten_does_not_sort_between_one_and_two(tmp_path):
    """Lexicographic order would put q10 second and show the wrong table."""
    z = _zip_of(tmp_path, "req_1_q1_x.csv", "req_1_q2_x.csv", "req_1_q10_x.csv")
    assert _statement_members(z) == [
        "req_1_q1_x.csv", "req_1_q2_x.csv", "req_1_q10_x.csv"]


def test_reading_a_plain_csv(tmp_path):
    p = _csv(tmp_path / "req_1_q1_x.csv", ["a"], ["only"])
    with _open_statement(p, 1) as fh:
        assert [r for r in csv.reader(fh)] == [["a"], ["only"]]


def test_reading_each_statement_of_a_zip(tmp_path):
    z = _zip_of(tmp_path, "req_1_q1_x.csv", "req_1_q2_x.csv")
    for n in (1, 2):
        with _open_statement(z, n) as fh:
            rows = [r for r in csv.reader(fh)]
        assert rows == [["col"], [f"stmt{n}"]], f"statement {n}"


def test_first_statement_is_the_default_view(tmp_path):
    """What the existing frontend gets when it does not ask for one — a table
    instead of the 409 it used to receive."""
    z = _zip_of(tmp_path, "req_1_q1_x.csv", "req_1_q2_x.csv")
    with _open_statement(z, 1) as fh:
        assert next(csv.reader(fh)) == ["col"]


def test_out_of_range_statement_is_a_404(tmp_path):
    z = _zip_of(tmp_path, "req_1_q1_x.csv", "req_1_q2_x.csv")
    with pytest.raises(Exception) as e:
        with _open_statement(z, 3):
            pass
    assert "3" in str(e.value) and "2" in str(e.value)


def test_zip_with_no_csv_members_is_refused(tmp_path):
    z = tmp_path / "req_1_results_x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("notes.txt", "nothing tabular here")
    assert _statement_members(z) == []
    with pytest.raises(Exception):
        with _open_statement(z, 1):
            pass
