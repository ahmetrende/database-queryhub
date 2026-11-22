"""Every result of one SQL Server statement, not only the first.

One statement can send several results. A trigger without SET NOCOUNT ON
sends a row count for each statement it runs, and it can send rows. The
server sends them BEFORE the count of the statement that fired the trigger:
an UPDATE of 2 rows whose trigger inserts 6 audit rows sends 6, then 2
(measured on SQL Server 2022, see tests/test_integration_mssql.py).

The executor read the first result only. So it reported the trigger's count,
and an error after the first result never reached the requester. These tests
drive `_more_mssql_results` with a cursor that replays a result sequence.
The live module runs the same cases against a real server.
"""
from __future__ import annotations

import csv
import zipfile

import pytest

from queryhub import executor as ex
from queryhub import pii
from queryhub.web import routes_queries as rq


class _Results:
    """A pyodbc-like cursor over a list of results.

    Each item is ("count", n), ("rows", columns, rows) or ("error", exc).
    The cursor starts on the first item, as it does after execute().
    """

    def __init__(self, items):
        self._items = list(items)
        self._pos = 0
        self._load()

    def _load(self):
        item = self._items[self._pos]
        if item[0] == "error":
            raise item[1]
        if item[0] == "count":
            self.description = None
            self.rowcount = item[1]
            self._rows = iter(())
        else:
            self.description = [(c, str, None, None, None, None, True)
                                for c in item[1]]
            self.rowcount = -1
            self._rows = iter(item[2])

    def __iter__(self):
        return self._rows

    def fetchone(self):
        return next(self._rows, None)

    def nextset(self):
        if self._pos + 1 >= len(self._items):
            return False
        self._pos += 1
        self._load()
        return True


def _stmt(sql="UPDATE dbo.orders SET state = 2 WHERE id < 3", leading="UPDATE"):
    return type("S", (), {"rewritten": sql, "leading": leading, "raw": sql})()


@pytest.fixture(autouse=True)
def _files_and_masking(monkeypatch, tmp_path):
    monkeypatch.setattr(ex, "CSV_DIR", tmp_path)
    monkeypatch.setattr(pii, "is_enabled", lambda: True)
    monkeypatch.setattr(pii, "_load_column_patterns",
                        lambda: [("email_address", "email", "substring", ())])
    monkeypatch.setattr(pii, "_load_exemptions", lambda *a, **k: [])
    return tmp_path


def _first_count(n, index=1, leading="UPDATE"):
    """The statement's first result as _execute_main_statement leaves it for a
    row count: no result set, the count clamped at 0."""
    return ex._StmtResult(index=index, leading=leading, rowcount=max(n, 0),
                          snippet="update dbo.orders set state = 2")


def _first_set(tmp_path, rows=1, index=1):
    path = tmp_path / f"req_7_q{index}_20261120T000000Z.csv"
    path.write_text("id\n" + "".join(f"{i}\n" for i in range(rows)))
    return ex._StmtResult(index=index, leading="SELECT", rowcount=rows,
                          has_result_set=True, csv_path=path,
                          snippet="select id from t")


def _more(cur, first, *, wants_result=True, max_rows=100,
          max_csv_bytes=1_000_000, stmt=None):
    return ex._more_mssql_results(
        cur, first, stmt or _stmt(), request_id=7, wants_result=wants_result,
        max_rows=max_rows, max_csv_bytes=max_csv_bytes,
        requester_id="U0AB12CD34", unmasked=False, result_format="csv",
        target_id=900001, database="shop")


def _texts(res):
    return [n["text"] for n in res.notices]


# --- row counts ---------------------------------------------------------------


def test_the_statement_count_is_the_last_one_and_the_trigger_count_goes_to_messages():
    first = _first_count(6)
    extra = _more(_Results([("count", 6), ("count", 2)]), first)
    assert extra == []
    assert first.rowcount == 2
    assert _texts(first) == ["(6 rows affected)"]


def test_a_statement_with_one_result_is_unchanged():
    first = _first_count(4)
    assert _more(_Results([("count", 4)]), first) == []
    assert first.rowcount == 4
    assert first.notices == []


def test_an_instead_of_trigger_keeps_the_outer_statement_count():
    # Measured: INSERT of 1 row through an INSTEAD OF trigger with two inserts
    # sends 1, 1, 1. The last one is the INSERT's own.
    first = _first_count(1, leading="INSERT")
    _more(_Results([("count", 1), ("count", 1), ("count", 1)]), first)
    assert first.rowcount == 1
    assert _texts(first) == ["(1 row affected)", "(1 row affected)"]


def test_a_count_the_driver_cannot_give_is_not_reported():
    first = _first_count(3)
    _more(_Results([("count", 3), ("count", -1)]), first)
    assert first.rowcount == 3
    assert first.notices == []


# --- result sets --------------------------------------------------------------


def test_rows_a_trigger_returns_become_a_table_of_their_own(tmp_path):
    first = _first_count(6)
    cur = _Results([("count", 6), ("rows", ["msg", "n"], [("trigger says", 2)]),
                    ("count", 2)])
    extra = _more(cur, first)
    assert first.rowcount == 2                  # the UPDATE's own count
    assert first.csv_path is None
    assert [(r.index, r.set_no, r.rowcount) for r in extra] == [(1, 1, 1)]
    assert "_q1s1_" in extra[0].csv_path.name
    with extra[0].csv_path.open(newline="") as fh:
        assert list(csv.reader(fh)) == [["msg", "n"], ["trigger says", "2"]]


def test_later_sets_of_a_read_are_numbered_after_the_first(tmp_path):
    first = _first_set(tmp_path)
    cur = _Results([("rows", ["id"], [(0,)]),
                    ("rows", ["a"], [("x",)]),
                    ("rows", ["b"], [("y",), ("z",)])])
    extra = _more(cur, first)
    assert [(r.set_no, r.rowcount) for r in extra] == [(2, 1), (3, 2)]
    assert ["_q1s2_" in extra[0].csv_path.name,
            "_q1s3_" in extra[1].csv_path.name] == [True, True]
    assert first.rowcount == 1 and first.notices == []


def test_a_later_set_is_masked_like_the_first(tmp_path):
    first = _first_count(1)
    cur = _Results([("count", 1),
                    ("rows", ["id", "email_address"], [(1, "jordan.ray@example.com")])])
    extra = _more(cur, first)
    body = extra[0].csv_path.read_text()
    assert "jordan.ray@example.com" not in body
    assert extra[0].pii_masked


def test_an_empty_later_set_leaves_no_file(tmp_path):
    first = _first_count(1)
    extra = _more(_Results([("count", 1), ("rows", ["id"], [])]), first)
    assert extra[0].has_result_set and extra[0].rowcount == 0
    assert extra[0].csv_path is None
    assert list(tmp_path.glob("*.csv")) == []


def test_without_wants_result_a_later_set_is_counted_not_written(tmp_path):
    first = _first_count(1)
    extra = _more(_Results([("count", 1), ("rows", ["id"], [(1,), (2,)])]),
                  first, wants_result=False)
    assert extra[0].rowcount == 2 and extra[0].csv_path is None
    assert list(tmp_path.glob("*.csv")) == []


def test_past_the_limit_a_set_is_dropped_and_messages_say_so(monkeypatch, tmp_path):
    monkeypatch.setattr(ex.cfg, "get_int",
                        lambda k, d=None: 2 if k == "max_result_sets" else d)
    first = _first_set(tmp_path)
    cur = _Results([("rows", ["id"], [(0,)])]
                   + [("rows", ["n"], [(i,)]) for i in range(3)])
    extra = _more(cur, first)
    assert [r.set_no for r in extra] == [2]
    assert _texts(first) == [
        "The statement sent 2 more result sets. QueryHub keeps 2 per statement."]


def test_the_sets_of_one_statement_share_one_size_limit(tmp_path):
    first = _first_set(tmp_path, rows=40)       # about 100 bytes on disk
    used = first.csv_path.stat().st_size
    cur = _Results([("rows", ["id"], [(0,)]),
                    ("rows", ["v"], [("x" * 50,)]),
                    ("rows", ["w"], [("y",)])])
    extra = _more(cur, first, max_csv_bytes=used + 20)
    # Dropped, not failed: the write is already done, and a request that says
    # "failed" invites someone to run it again.
    assert extra == []
    assert not first.truncated_size
    assert sorted(p.name for p in tmp_path.glob("*.csv")) == [first.csv_path.name]
    assert _texts(first) == ["The statement sent 2 more result sets past the "
                             "size limit. QueryHub did not keep them."]


def test_a_trigger_set_that_came_first_moves_to_result_set_1(tmp_path):
    """Measured: a trigger with SET NOCOUNT ON that selects sends its set
    FIRST, then the UPDATE's own count."""
    first = _first_set(tmp_path)
    first.leading = "UPDATE"
    cur = _Results([("rows", ["note"], [("from trigger",)]), ("count", 2)])
    extra = _more(cur, first)
    assert (first.has_result_set, first.rowcount, first.csv_path) == (False, 2, None)
    assert first.notices == []
    assert [(r.set_no, r.rowcount) for r in extra] == [(1, 1)]
    assert "_q1s1_" in extra[0].csv_path.name and extra[0].csv_path.is_file()
    assert [p.name for p in tmp_path.glob("*.csv")] == [extra[0].csv_path.name]


def test_an_output_clause_stays_the_write_s_own_result(tmp_path):
    # Measured: UPDATE ... OUTPUT sends one set and no row count after it.
    first = _first_set(tmp_path, rows=2)
    first.leading = "UPDATE"
    assert _more(_Results([("rows", ["id"], [(1,), (2,)])]), first) == []
    assert (first.has_result_set, first.rowcount, first.set_no) == (True, 2, 0)
    assert "_q1_" in first.csv_path.name


def test_a_moved_set_past_the_size_limit_is_dropped_not_failed(tmp_path):
    first = _first_set(tmp_path)
    first.leading = "UPDATE"
    first.truncated_size = True
    extra = _more(_Results([("rows", ["note"], [("x",)]), ("count", 2)]), first)
    assert extra == [] and first.rowcount == 2 and not first.truncated_size
    assert list(tmp_path.glob("*.csv")) == []
    assert _texts(first) == ["The statement sent 1 more result set past the "
                             "size limit. QueryHub did not keep it."]


def test_a_later_set_gets_no_masking_exemption(monkeypatch, tmp_path):
    """The exemption rows decide from the tables NAMED in the statement. A
    trigger's rows come from other tables, so an exemption on the written
    table must not unmask them."""
    asked = []
    monkeypatch.setattr(pii, "exemption_decision",
                        lambda *a, **k: asked.append(a) or (True, set()))
    monkeypatch.setattr(pii, "system_catalog_only", lambda *a, **k: True)
    first = _first_count(1)
    extra = _more(_Results([("count", 1),
                            ("rows", ["email_address"], [("jordan.ray@example.com",)])]),
                  first)
    assert "jordan.ray@example.com" not in extra[0].csv_path.read_text()
    assert not extra[0].pii_exempt
    assert asked == []


def test_a_sql_server_write_writes_its_first_set_with_no_exemption():
    import inspect
    # The statement loop lives in _mssql_statements since the B01/B16 change.
    src = inspect.getsource(ex._run_mssql) + inspect.getsource(ex._mssql_statements)
    assert src.count("exemptions=s.leading.upper() not in _MSSQL_NO_EXEMPTION") == 2
    # A super-admin's EXEC reads tables that it does not name, like a write's
    # trigger, so its sets get no exemption either.
    assert {"INSERT", "UPDATE", "DELETE", "MERGE", "EXEC", "EXECUTE"} <= ex._MSSQL_NO_EXEMPTION


def test_an_error_after_the_first_result_fails_the_statement():
    # Measured: a trigger that throws after its first insert. The executor
    # used to report the first count as a success; the server had rolled the
    # statement back.
    first = _first_count(2)
    cur = _Results([("count", 2), ("rows", ["msg"], [("trigger says",)]),
                    ("error", RuntimeError("v = 99 is not allowed"))])
    with pytest.raises(RuntimeError, match="v = 99"):
        _more(cur, first)


# --- the run record and the messages ----------------------------------------


def test_run_notes_count_statements_not_tables(tmp_path):
    a = _first_count(2)
    b = ex._StmtResult(index=1, leading="UPDATE", set_no=1, has_result_set=True)
    c = ex._StmtResult(index=2, leading="SELECT", rowcount=3, has_result_set=True)
    notes = ex._run_notes([a, b, c])
    assert [s["i"] for s in notes["statements"]] == [1, 2]
    assert ex._run_shape([a, b, c]) == "2 statements, 1 more result set"
    assert ex._run_shape([a, c]) == "2 statements"
    assert ex._run_shape([a]) == "1 statement"


# --- the web: which table is which ------------------------------------------


def _zip(tmp_path, names):
    z = tmp_path / "req_7_results_20261120T000000Z.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for n in names:
            zf.writestr(n, "c\n1\n")
    return z


def test_result_sets_sort_after_their_statement(tmp_path):
    z = _zip(tmp_path, ["req_7_q2_t.csv", "req_7_q1s10_t.csv", "req_7_q1s2_t.csv",
                        "req_7_q1_t.csv"])
    assert rq._statement_members(z) == [
        "req_7_q1_t.csv", "req_7_q1s2_t.csv", "req_7_q1s10_t.csv", "req_7_q2_t.csv"]


_NOTES = {"run_notes": {"statements": [
    {"i": 1, "leading": "SELECT", "snippet": "select a from t"},
    {"i": 2, "leading": "UPDATE", "snippet": "update t set a = 1"},
    {"i": 3, "leading": "SELECT", "snippet": "select b from t"},
]}}


def test_labels_follow_the_tables_when_every_statement_made_one():
    labels = rq._member_labels(_NOTES, ["req_7_q1_t.csv", "req_7_q2_t.csv",
                                        "req_7_q3_t.csv"])
    assert labels == rq._statement_labels(_NOTES, 3)


def test_a_statement_with_no_table_does_not_shift_the_labels():
    labels = rq._member_labels(_NOTES, ["req_7_q1_t.csv", "req_7_q3_t.csv"])
    assert labels == [
        {"n": 1, "kind": "SELECT", "snippet": "select a from t"},
        {"n": 2, "kind": "SELECT", "snippet": "select b from t", "stmt": 3},
    ]


def test_each_result_set_is_labelled_with_its_number():
    labels = rq._member_labels(_NOTES, ["req_7_q1_t.csv", "req_7_q1s2_t.csv",
                                        "req_7_q2s1_t.csv"])
    assert labels == [
        {"n": 1, "kind": "SELECT", "snippet": "select a from t", "set": 1},
        {"n": 2, "kind": "SELECT", "snippet": "select a from t", "stmt": 1,
         "set": 2},
        {"n": 3, "kind": "UPDATE", "snippet": "update t set a = 1", "stmt": 2,
         "set": 1},
    ]


def test_a_member_without_a_statement_number_keeps_the_old_labels():
    assert rq._member_labels(_NOTES, ["export.csv"]) == rq._statement_labels(_NOTES, 1)
