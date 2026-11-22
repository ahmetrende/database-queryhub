"""Row totals, the `capped` flag and Excel results on the result route.

A multi-table result used to report only the rows of its first page as
`total`, so the client never paged past them. An `.xlsx` result (asked for in
Slack) used to be opened as UTF-8 text and failed.
"""
import csv
import zipfile

import pytest

from queryhub.web import routes_queries

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Example"}


@pytest.fixture
def route(monkeypatch):
    state = {"row": {}, "page": 3, "path": None}
    monkeypatch.setattr(routes_queries.deps, "require_whitelisted", lambda c: None)
    monkeypatch.setattr(routes_queries, "_own_request", lambda rid, uid: state["row"])
    monkeypatch.setattr(routes_queries, "_result_file", lambda row: state["path"])
    monkeypatch.setattr(routes_queries, "_masked_pii_cols", lambda *a, **k: [])
    monkeypatch.setattr(routes_queries, "_ran_unmasked", lambda rid: False)
    monkeypatch.setattr(routes_queries.mapping, "run_ms", lambda row: 1)
    monkeypatch.setattr(
        routes_queries.cfg, "get_int",
        lambda key, default: state["page"] if key == "web_result_max_rows" else default)

    def call(path, row_extra=None, statement=1):
        state["path"] = path
        state["row"] = {"id": 1, "status": "completed",
                        "csv_file_path": str(path), "query": "select 1",
                        **(row_extra or {})}
        return routes_queries.query_result(1, statement, claims=CLAIMS)
    return call


def _csv_text(n):
    return "a\n" + "".join(f"{i}\n" for i in range(n))


def test_multi_member_total_is_the_stored_count_of_the_chosen_table(route, tmp_path):
    z = tmp_path / "req_1_results_x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("req_1_q1_x.csv", _csv_text(7))
        zf.writestr("req_1_q2_x.csv", _csv_text(5))
    first = route(z, {"row_count": 12}, statement=1)
    assert first["total"] == 7           # not the summed row_count, not the page
    assert len(first["rows"]) == 3
    assert first["truncated"] is True
    second = route(z, {"row_count": 12}, statement=2)
    assert second["total"] == 5


def test_single_member_total_is_the_recorded_count(route, tmp_path):
    p = tmp_path / "req_1_x.csv"
    p.write_text(_csv_text(7), encoding="utf-8")
    out = route(p, {"row_count": 7})
    assert out["total"] == 7
    assert len(out["rows"]) == 3
    assert out["truncated"] is True
    assert out["capped"] is False        # a short page is not a cap


def test_capped_is_sent_from_the_stored_cap_flag(route, tmp_path):
    p = tmp_path / "req_1_x.csv"
    p.write_text(_csv_text(2), encoding="utf-8")
    out = route(p, {"row_count": 2, "truncated": True})
    assert out["capped"] is True
    assert out["truncated"] is True


def test_xlsx_result_opens_as_a_table(route, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "req_1_x.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["id", "name"])
    ws.append([1, "Sule"])
    ws.append([2, None])
    wb.save(p)
    out = route(p, {"row_count": 2})
    assert out["kind"] == "table"
    assert out["cols"] == ["id", "name"]
    assert out["rows"] == [{"id": "1", "name": "Sule"}, {"id": "2", "name": ""}]
    assert out["total"] == 2


def test_xlsx_rows_page_through_open_statement(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "req_1_x.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["a"])
    wb.active.append(["x"])
    wb.save(p)
    with routes_queries._open_statement(p, 1) as fh:
        assert list(csv.reader(fh)) == [["a"], ["x"]]
