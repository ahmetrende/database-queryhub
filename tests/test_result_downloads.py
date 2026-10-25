"""Downloads of a stored result in other shapes, and the CSV with a BOM.

Excel reads a CSV in the local code page unless the file starts with a UTF-8
byte order mark, so Turkish letters arrive broken. The BOM goes into one
download only: the stored file feeds the grid, Slack and Copy as CSV, and a BOM
there would stick to the first column name.

The other downloads read the stored, already masked CSV and change its shape.
"""
import asyncio
import zipfile

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from queryhub.web import routes_queries

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Example"}
CSV_TEXT = "id,name\n1,\u015eule\n2,\n"
BOM = b"\xef\xbb\xbf"


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """A completed request whose result is a plain CSV. Returns (path, audit)."""
    p = tmp_path / "req_42_20260925T070000Z.csv"
    p.write_bytes(CSV_TEXT.encode("utf-8"))
    state = {"row": {"status": "completed", "csv_file_path": str(p)}, "audit": [],
             "cap": 100000}
    monkeypatch.setattr(routes_queries.deps, "require_whitelisted", lambda c: None)
    monkeypatch.setattr(routes_queries, "_own_request", lambda rid, uid: state["row"])
    monkeypatch.setattr(routes_queries.audit_mod, "log",
                        lambda *a, **k: state["audit"].append(a[3:]))
    monkeypatch.setattr(
        routes_queries.cfg, "get_int",
        lambda key, default: state["cap"] if key == "result_export_max_rows" else default)
    return p, state


def _body(resp) -> bytes:
    async def collect():
        return b"".join([c async for c in resp.body_iterator])
    return asyncio.run(collect())


def _zip(tmp_path, state, members):
    z = tmp_path / "req_42_20260925T070000Z.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for n, text in members.items():
            zf.writestr(n, text)
    state["row"] = {"status": "completed", "csv_file_path": str(z)}
    return z


# ---- CSV with a BOM ---------------------------------------------------------

def test_bom_download_is_the_stored_bytes_behind_a_bom(stored):
    p, state = stored
    before = p.read_bytes()
    resp = routes_queries.query_result_csv(42, None, 1, claims=CLAIMS)
    assert _body(resp) == BOM + before
    assert resp.headers["content-length"] == str(len(before) + 3)
    assert resp.headers["content-disposition"] == 'attachment; filename="queryhub_result_42.csv"'
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert p.read_bytes() == before                      # the stored file does not change
    assert state["audit"] == [("web_result_downloaded", {"file": p.name, "bom": True})]


def test_the_plain_download_does_not_change(stored):
    p, state = stored
    resp = routes_queries.query_result_csv(42, None, claims=CLAIMS)
    assert isinstance(resp, FileResponse)
    assert resp.path == p
    assert state["audit"] == [("web_result_downloaded", {"file": p.name})]


def test_bom_on_one_table_of_an_archive(stored, tmp_path):
    p, state = stored
    _zip(tmp_path, state, {"req_42_q1_x.csv": "a\n1\n", "req_42_q2_x.csv": "b\n\u015e\n"})
    resp = routes_queries.query_result_csv(42, 2, 1, claims=CLAIMS)
    assert _body(resp) == BOM + "b\n\u015e\n".encode("utf-8")
    assert resp.headers["content-disposition"] == 'attachment; filename="queryhub_result_42_q2.csv"'
    assert state["audit"][-1] == ("web_result_downloaded",
                                  {"file": "req_42_20260925T070000Z.zip",
                                   "statement": 2, "bom": True})
    assert resp.background is not None          # it removes the temp copy
    asyncio.run(resp.background())


def test_bom_is_ignored_for_the_whole_archive(stored, tmp_path):
    p, state = stored
    z = _zip(tmp_path, state, {"req_42_q1_x.csv": "a\n1\n"})
    resp = routes_queries.query_result_csv(42, None, 1, claims=CLAIMS)
    assert isinstance(resp, FileResponse)
    assert resp.path == z
    assert state["audit"] == [("web_result_downloaded", {"file": z.name})]


# ---- JSON, Markdown, SQL ----------------------------------------------------

def test_json_download(stored):
    p, state = stored
    resp = routes_queries.query_result_json(42, None, claims=CLAIMS)
    assert _body(resp).decode("utf-8") == (
        '{"columns":["id","name"],"rows":[\n'
        '{"id":"1","name":"\u015eule"},\n'
        '{"id":"2","name":""}\n]}')
    assert resp.media_type == "application/json"
    assert resp.headers["content-disposition"] == 'attachment; filename="queryhub_result_42.json"'
    assert state["audit"] == [("web_result_downloaded", {"format": "json", "file": p.name})]


def test_markdown_download(stored):
    resp = routes_queries.query_result_markdown(42, None, claims=CLAIMS)
    assert _body(resp).decode("utf-8") == (
        "| id | name |\n| --- | --- |\n| 1 | \u015eule |\n| 2 |  |\n")
    assert resp.headers["content-disposition"] == 'attachment; filename="queryhub_result_42.md"'


def test_sql_download(stored):
    resp = routes_queries.query_result_sql(42, None, claims=CLAIMS)
    assert _body(resp).decode("utf-8").splitlines()[2:] == [
        'INSERT INTO "table_name" ("id", "name") VALUES (\'1\', \'\u015eule\');',
        'INSERT INTO "table_name" ("id", "name") VALUES (\'2\', NULL);']
    assert resp.headers["content-disposition"] == 'attachment; filename="queryhub_result_42.sql"'


def test_two_columns_with_one_name_stay_apart(stored):
    p, _ = stored
    p.write_text("a,a\n1,2\n")
    resp = routes_queries.query_result_json(42, None, claims=CLAIMS)
    assert _body(resp).decode() == '{"columns":["a","a (2)"],"rows":[\n{"a":"1","a (2)":"2"}\n]}'


def test_a_table_of_an_archive_is_chosen_with_statement(stored, tmp_path):
    p, state = stored
    _zip(tmp_path, state, {"req_42_q1_x.csv": "a\n1\n", "req_42_q2_x.csv": "b\n2\n"})
    resp = routes_queries.query_result_json(42, 2, claims=CLAIMS)
    assert _body(resp).decode() == '{"columns":["b"],"rows":[\n{"b":"2"}\n]}'
    assert resp.headers["content-disposition"] == 'attachment; filename="queryhub_result_42_q2.json"'


def test_an_archive_without_statement_is_refused(stored, tmp_path):
    _, state = stored
    _zip(tmp_path, state, {"req_42_q1_x.csv": "a\n1\n"})
    with pytest.raises(HTTPException) as e:
        routes_queries.query_result_json(42, None, claims=CLAIMS)
    assert e.value.status_code == 409


def test_a_result_that_is_not_ready_is_refused(stored):
    _, state = stored
    state["row"] = {"status": "pending", "csv_file_path": None}
    with pytest.raises(HTTPException) as e:
        routes_queries.query_result_markdown(42, None, claims=CLAIMS)
    assert e.value.status_code == 409


def test_a_stored_workbook_is_refused(stored, tmp_path):
    _, state = stored
    x = tmp_path / "req_42.xlsx"
    x.write_bytes(b"PK")
    state["row"] = {"status": "completed", "csv_file_path": str(x)}
    with pytest.raises(HTTPException) as e:
        routes_queries.query_result_sql(42, None, claims=CLAIMS)
    assert e.value.status_code == 409


def test_a_result_over_the_row_cap_answers_413_and_the_cap_itself_passes(stored):
    p, state = stored
    p.write_text("a\n1\n2\n3\n")
    state["cap"] = 3
    assert _body(routes_queries.query_result_json(42, None, claims=CLAIMS)).count(b"\n") == 4
    state["cap"] = 2
    with pytest.raises(HTTPException) as e:
        routes_queries.query_result_json(42, None, claims=CLAIMS)
    assert e.value.status_code == 413
    assert "2 rows" in e.value.detail["message"]


def test_an_empty_file_is_refused(stored):
    p, _ = stored
    p.write_text("")
    with pytest.raises(HTTPException) as e:
        routes_queries.query_result_markdown(42, None, claims=CLAIMS)
    assert e.value.status_code == 409
