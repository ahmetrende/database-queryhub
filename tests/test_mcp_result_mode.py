"""What a program may read of a result: `mcp_result_mode`.

The default is `metadata_only`. Claude reads the status, the row count and the
column names. It reads no value of the result. The person opens the result in
the QueryHub web UI. `masked_rows` is the other mode, and it sends the masked
rows too.

These tests use a result file that holds a value no real data holds
(`CANARY`). Every tool answer is searched for it. A canary in an answer in the
default mode is a leak, whichever tool, field or path carried it.

Nothing here may reach a database.
"""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from queryhub import db
from queryhub.mcp_server import catalog, policy, tools
from queryhub.web import routes_queries

ME = "U0EXAMPLE001"
CANARY = "CANARY-4f9a-value"


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)


def _settings(monkeypatch, **values):
    monkeypatch.setattr(policy.cfg, "get_setting",
                        lambda key, default=None: values.get(key, default))


def _csv(tmp_path: Path, name="result.csv") -> Path:
    p = tmp_path / name
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["email", "orders"])
        w.writerow([CANARY, "7"])
        w.writerow(["second@example.test", "9"])
    return p


def _row(**over):
    row = {"id": 41, "status": "completed", "requester_slack_id": ME,
           "target_server_id": 7, "database_name": "ledger",
           "query": "SELECT email, orders FROM users", "row_count": 2,
           "truncated": False, "required_tier": "ro", "error_message": None,
           "csv_file_path": None, "created_at": datetime.now(timezone.utc),
           "completed_at": datetime.now(timezone.utc)}
    row.update(over)
    return row


@pytest.fixture
def req(monkeypatch):
    st = {"row": _row(), "unmasked": False}
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools, "_own", lambda rid, uid: st["row"])
    monkeypatch.setattr(routes_queries, "_ran_unmasked", lambda rid: st["unmasked"])
    _settings(monkeypatch, mcp_portal_url="https://portal.example.test/queryhub")
    return st


def _text(out) -> str:
    return json.dumps(out, default=str)


# ---------------------------------------------------------------------------
# the mode
# ---------------------------------------------------------------------------

def test_the_default_is_metadata_only(monkeypatch):
    _settings(monkeypatch)
    assert policy.result_mode() == "metadata_only"
    assert policy.rows_allowed() is False


@pytest.mark.parametrize("value", ["", "  ", "ROWS", "masked", "masked-rows", "on", "true", "all"])
def test_a_value_nobody_can_parse_reads_as_metadata_only(monkeypatch, value):
    """A typo must never be the value that sends rows out."""
    _settings(monkeypatch, mcp_result_mode=value)
    assert policy.result_mode() == "metadata_only"
    assert policy.rows_allowed() is False


def test_masked_rows_is_read_per_call(monkeypatch):
    _settings(monkeypatch, mcp_result_mode=" Masked_Rows ")
    assert policy.result_mode() == "masked_rows" and policy.rows_allowed() is True


def test_the_migration_seeds_the_closed_value():
    sql = (Path(__file__).resolve().parents[1] / "migrations"
           / "202_mcp_result_mode.sql").read_text(encoding="utf-8")
    assert "'mcp_result_mode', 'metadata_only'" in sql
    assert "ON CONFLICT (key) DO NOTHING" in sql


def test_list_connections_tells_claude_the_mode(monkeypatch):
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools.targets, "list_enabled", lambda: [])
    monkeypatch.setattr(tools.teams, "effective_grants_for_user", lambda uid, ids: {})
    monkeypatch.setattr(tools.db_scope, "catalog_databases_map", lambda ids: {})
    _settings(monkeypatch)
    assert tools.list_connections()["resultMode"] == "metadata_only"
    _settings(monkeypatch, mcp_result_mode="masked_rows")
    assert tools.list_connections()["resultMode"] == "masked_rows"


# ---------------------------------------------------------------------------
# fetch_result
# ---------------------------------------------------------------------------

def test_fetch_result_sends_counts_and_columns_and_no_row(req, tmp_path):
    req["row"] = _row(csv_file_path=str(_csv(tmp_path)))
    out = tools.fetch_result(41)
    assert out["columns"] == ["email", "orders"]
    assert out["rowCount"] == 2 and out["kind"] == "read" and out["truncated"] is False
    assert out["rowsHidden"] is True and "rows" not in out
    assert out["requestUrl"] == "https://portal.example.test/queryhub?request=41"
    assert CANARY not in _text(out)


def test_fetch_result_ignores_offset_and_limit(req, tmp_path):
    req["row"] = _row(csv_file_path=str(_csv(tmp_path)))
    for offset, limit in ((0, 1), (1, 200), (5, 50)):
        assert CANARY not in _text(tools.fetch_result(41, offset, limit))


def test_fetch_result_in_masked_rows_mode_returns_rows(req, tmp_path, monkeypatch):
    """The other half: the switch is what closed it."""
    _settings(monkeypatch, mcp_result_mode="masked_rows")
    req["row"] = _row(csv_file_path=str(_csv(tmp_path)))
    out = tools.fetch_result(41)
    assert out["rows"][0] == {"email": CANARY, "orders": "7"}
    assert "rowsHidden" not in out


def test_a_result_that_ran_without_masking_never_goes_out(req, tmp_path, monkeypatch):
    """A super-admin can run their own request unmasked. Its file holds the
    real values, so even masked_rows mode refuses it."""
    _settings(monkeypatch, mcp_result_mode="masked_rows")
    req["row"] = _row(csv_file_path=str(_csv(tmp_path)))
    req["unmasked"] = True
    with pytest.raises(tools.ToolError) as e:
        tools.fetch_result(41)
    assert e.value.code == "unmasked_result"
    assert CANARY not in e.value.message


def test_a_request_that_is_not_done_is_not_ready(req):
    req["row"] = _row(status="pending")
    with pytest.raises(tools.ToolError) as e:
        tools.fetch_result(41)
    assert e.value.code == "not_ready"


def test_a_write_reports_the_rows_it_changed(req):
    req["row"] = _row(required_tier="rw", row_count=3, csv_file_path=None,
                      query="UPDATE users SET a = 1 WHERE id < 4")
    out = tools.fetch_result(41)
    assert out["kind"] == "write" and out["rowCount"] == 3 and out["columns"] is None


def test_ddl_is_named_ddl(req):
    req["row"] = _row(required_tier="ddl", row_count=0)
    assert tools.fetch_result(41)["kind"] == "ddl"


def test_a_cut_read_says_the_real_count_is_higher(req, tmp_path):
    req["row"] = _row(row_count=1000, truncated=True, csv_file_path=str(_csv(tmp_path)))
    out = tools.fetch_result(41)
    assert out["truncated"] is True and out["rowCount"] == 1000
    assert "row limit" in out["note"] and "more rows" in out["note"]


def test_a_missing_file_gives_no_columns_and_no_error(req, tmp_path):
    req["row"] = _row(csv_file_path=str(tmp_path / "gone.csv"))
    assert tools.fetch_result(41)["columns"] is None


def test_a_result_with_a_file_for_each_statement_gives_no_columns(req, tmp_path):
    z = tmp_path / "result.zip"
    z.write_bytes(b"PK")
    req["row"] = _row(csv_file_path=str(z))
    assert tools.fetch_result(41)["columns"] is None


def test_an_empty_file_gives_an_empty_column_list(req, tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    req["row"] = _row(csv_file_path=str(p))
    assert tools.fetch_result(41)["columns"] == []


def test_no_address_means_no_link(req, tmp_path, monkeypatch):
    _settings(monkeypatch)
    req["row"] = _row(csv_file_path=str(_csv(tmp_path)))
    out = tools.fetch_result(41)
    assert "requestUrl" not in out and out["rowsHidden"] is True


# ---------------------------------------------------------------------------
# query_status
# ---------------------------------------------------------------------------

def test_status_of_a_finished_read_has_columns_and_no_row(req, tmp_path):
    req["row"] = _row(csv_file_path=str(_csv(tmp_path)))
    out = tools.query_status(41)
    assert out["status"] == "completed" and out["kind"] == "read"
    assert out["columns"] == ["email", "orders"] and out["rowsHidden"] is True
    assert "rows" not in out and CANARY not in _text(out)


def test_status_of_a_request_in_the_queue_has_no_result_fields(req):
    req["row"] = _row(status="pending", row_count=None, csv_file_path=None)
    out = tools.query_status(41)
    assert "columns" not in out and "rowsHidden" not in out
    assert out["requestUrl"].endswith("/queryhub?request=41")


def test_an_error_is_cleaned_before_it_goes_out(req):
    """A database can echo a value in an error. The default mode cleans it."""
    req["row"] = _row(status="failed", csv_file_path=None, row_count=None,
                      query="INSERT INTO users (email) VALUES ('a')",
                      error_message=f"duplicate key value violates unique constraint "
                                    f'"users_email_key" DETAIL: Key (email)=({CANARY}) '
                                    f"already exists.")
    out = tools.query_status(41)
    assert CANARY not in _text(out), out["error"]
    assert out["error"], "the error is still there, only cleaned"


@pytest.mark.parametrize("pii", ["on", "off"])
def test_a_value_echoed_in_the_main_line_is_hidden_whatever_the_pii_switch(req, monkeypatch, pii):
    """The database puts the value in the message itself, not in a DETAIL
    line that the clean always drops. The default mode sends no row, so it
    hides that value with PII masking off too."""
    _settings(monkeypatch, pii_masking_enabled=pii)
    req["row"] = _row(status="failed", csv_file_path=None, row_count=None,
                      query="SELECT full_name::int FROM customers WHERE id = 1",
                      error_message=f'invalid input syntax for type integer: "{CANARY}"')
    out = tools.query_status(41)
    assert CANARY not in _text(out), out["error"]
    assert out["error"].startswith("Invalid input syntax for type integer")


def test_an_error_is_left_alone_in_masked_rows_mode(req, monkeypatch):
    _settings(monkeypatch, mcp_result_mode="masked_rows")
    req["row"] = _row(status="failed", error_message="relation \"nope\" does not exist")
    assert tools.query_status(41)["error"] == "relation \"nope\" does not exist"


# ---------------------------------------------------------------------------
# the wait inside submit_query
# ---------------------------------------------------------------------------

def test_the_inline_wait_sends_no_row(req, tmp_path, monkeypatch):
    """`submit_query` waits for an auto-approved result and used to return its
    first rows. In the default mode it returns the counts."""
    finished = _row(csv_file_path=str(_csv(tmp_path)))
    monkeypatch.setattr(tools.db, "fetch_one", lambda sql, params=None: finished)
    out = tools._await_result(41, 1)
    assert out["status"] == "completed" and out["columns"] == ["email", "orders"]
    assert out["rowCount"] == 2 and out["rowsHidden"] is True
    assert "rows" not in out and "moreRows" not in out and CANARY not in _text(out)


def test_the_inline_wait_sends_rows_in_masked_rows_mode(req, tmp_path, monkeypatch):
    _settings(monkeypatch, mcp_result_mode="masked_rows")
    finished = req["row"] = _row(csv_file_path=str(_csv(tmp_path)))
    monkeypatch.setattr(tools.db, "fetch_one", lambda sql, params=None: finished)
    out = tools._await_result(41, 1)
    assert out["rows"][0]["email"] == CANARY


def test_the_inline_wait_reports_a_write_without_a_file(req, monkeypatch):
    finished = _row(required_tier="rw", row_count=5, csv_file_path=None)
    monkeypatch.setattr(tools.db, "fetch_one", lambda sql, params=None: finished)
    out = tools._await_result(41, 1)
    assert out["kind"] == "write" and out["rowCount"] == 5


# ---------------------------------------------------------------------------
# no path leaks the canary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier,rows", [("ro", 2), ("rw", 2), ("ddl", 0)])
def test_no_tool_answer_holds_the_canary_in_the_default_mode(req, tmp_path, monkeypatch, tier, rows):
    row = _row(required_tier=tier, row_count=rows, csv_file_path=str(_csv(tmp_path)))
    req["row"] = row
    monkeypatch.setattr(tools.db, "fetch_one", lambda sql, params=None: row)
    answers = [tools.fetch_result(41), tools.fetch_result(41, 1, 1), tools.query_status(41),
               tools._await_result(41, 1)]
    for answer in answers:
        assert CANARY not in _text(answer), answer


# ---------------------------------------------------------------------------
# what Claude is told
# ---------------------------------------------------------------------------

def test_the_descriptions_name_the_modes():
    text = json.dumps(catalog.listing())
    assert "`resultMode`" in text and "`metadata_only`" in text and "`masked_rows`" in text
    assert "paste the result" in text


def test_the_tool_descriptions_say_that_rows_need_masked_rows():
    assert "masked_rows" in catalog.BY_NAME["fetch_result"].description
    assert "resultMode" in catalog.BY_NAME["list_connections"].description
