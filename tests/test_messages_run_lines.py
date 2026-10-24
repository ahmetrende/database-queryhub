"""The success lines of the Messages tab (2026-10-24, batch 2).

A finished request used to end with "Done — 12 row(s).". It now says:

- "184 rows in 412 ms." when the request has a row count and a run time;
- "1 PII column masked: iban." when the run masked columns (names only, never a
  value);
- "Ran on <replica name>, about 2.3 s behind." when an automatic route used a
  replica and the reader may see replica names (a super-admin).

A request with a missing field keeps the old wording. The data comes from the
request row and the stored result: the row count, `executed_at` and
`completed_at`, `run_notes.replica`, and the header line of each stored table.
All names, SQL and values here are invented.
"""
from __future__ import annotations

import inspect
import json
import logging
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from queryhub import admins, pii, replicas
from queryhub.web import app as web_app
from queryhub.web import deps, mapping, routes_queries

ME = "U0EXAMPLE001"
T0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=timezone.utc)
SECRET = "TR00-EXAMPLE-IBAN-9999"       # a masked value: it must never be printed


def _row(**over):
    row = {"id": 77, "query": "SELECT id, iban FROM accounts", "status": "completed",
           "database_name": "app", "requester_slack_id": ME, "target_server_id": 7,
           "engine": "postgres", "executed_tier": "ro", "required_tier": "ro",
           "executed_target_id": None, "run_on": None,
           "created_at": T0, "decided_at": T0, "executed_at": T0,
           "completed_at": T0 + timedelta(milliseconds=412),
           "row_count": 184, "scheduled_for": None, "error_message": None,
           "run_notes": None, "decided_by_slack_id": "AUTO",
           "decided_by_name": "auto-approved (grant #12)"}
    row.update(over)
    return row


def _texts(row, **kw):
    return [m["text"] for m in mapping.status_messages(row, [], **kw)]


# ---- the success line -------------------------------------------------------------

@pytest.mark.parametrize("rows,ms,text", [
    (184, 412, "184 rows in 412 ms."),
    (1, 7, "1 row in 7 ms."),
    (0, 3, "0 rows in 3 ms."),
    (1204, 5120, "1,204 rows in 5,120 ms."),
    (5, 0, "5 rows in 0 ms."),
])
def test_the_success_line_has_the_rows_and_the_time(rows, ms, text):
    assert mapping.done_line(rows, ms) == text


@pytest.mark.parametrize("rows,ms,text", [
    (184, None, "Done — 184 row(s)."),          # no run time: the old wording
    (None, 412, "Done — ? row(s)."),            # no row count: the old wording
    (None, None, "Done — ? row(s)."),
])
def test_a_missing_field_keeps_the_old_wording(rows, ms, text):
    assert mapping.done_line(rows, ms) == text


def test_the_feed_ends_with_the_success_line():
    texts = _texts(_row())
    assert texts[-1] == "184 rows in 412 ms."
    assert "Done — 184 row(s)." not in texts


def test_a_row_with_no_run_times_keeps_the_old_line():
    texts = _texts(_row(executed_at=None, completed_at=None))
    assert texts[-1] == "Done — 184 row(s)."


def test_a_row_with_no_count_keeps_the_old_line():
    assert _texts(_row(row_count=None))[-1] == "Done — ? row(s)."


def test_a_request_that_did_not_finish_has_no_success_line():
    for status in ("failed", "rejected", "pending"):
        texts = _texts(_row(status=status, error_message="boom", decided_at=None
                            if status == "pending" else T0, decision_reason="no"),
                       masked_columns=["iban"])
        assert not any("rows in" in t or "Done —" in t or "PII column" in t for t in texts), status


# ---- the masked-columns line --------------------------------------------------------

@pytest.mark.parametrize("cols,text", [
    (["iban"], "1 PII column masked: iban."),
    (["iban", "phone"], "2 PII columns masked: iban, phone."),
    (["iban", "iban", "phone"], "2 PII columns masked: iban, phone."),          # once each
    (["a", "b", "c", "d", "e"], "5 PII columns masked: a, b, c, d, e."),
    (["a", "b", "c", "d", "e", "f", "g"], "7 PII columns masked: a, b, c, d, e and 2 more."),
    (["  iban \n", "x\ty"], "2 PII columns masked: iban, x y."),               # one line
])
def test_the_masked_line_names_the_columns(cols, text):
    assert mapping.masked_line(cols) == text


@pytest.mark.parametrize("cols", [None, [], ["", "  ", "\n"], ()])
def test_no_masked_column_means_no_line(cols):
    assert mapping.masked_line(cols) is None


def test_a_long_column_name_is_cut():
    line = mapping.masked_line(["x" * 200])
    assert line == "1 PII column masked: " + "x" * 60 + "."


def test_the_masked_line_follows_the_success_line():
    texts = _texts(_row(), masked_columns=["iban"])
    assert texts[-2:] == ["184 rows in 412 ms.", "1 PII column masked: iban."]
    kinds = [m["kind"] for m in mapping.status_messages(_row(), [], masked_columns=["iban"])]
    assert kinds[-2:] == ["ok", "info"]


def test_no_masked_columns_adds_no_line():
    base = _texts(_row())
    assert _texts(_row(), masked_columns=[]) == base
    assert _texts(_row(), masked_columns=None) == base
    assert not any("PII" in t for t in base)


def test_the_masked_line_never_carries_a_value():
    """The builder takes NAMES. A statement that holds a masked value in its
    text, and a row that holds one, do not reach the line."""
    row = _row(query=f"SELECT iban FROM accounts WHERE iban = '{SECRET}'")
    texts = _texts(row, masked_columns=["iban"])
    assert "1 PII column masked: iban." in texts
    assert not any(SECRET in t for t in texts)
    assert SECRET not in mapping.masked_line(["iban"])


# ---- the names come from the header of the stored result -----------------------------

def _pii(monkeypatch, masked_index=None, enabled=True):
    """pii as the executor left it: column `masked_index` is masked."""
    monkeypatch.setattr(pii, "is_enabled", lambda: enabled)
    monkeypatch.setattr(pii, "exemption_decision", lambda *a, **k: (False, set()))
    monkeypatch.setattr(pii, "exemption_namescan", lambda *a, **k: set())
    monkeypatch.setattr(pii, "column_pii_map",
                        lambda cols, sql=None, **k: {} if masked_index is None else
                        {i: "iban" for i, c in enumerate(cols) if c in masked_index})


def _csv(tmp_path, header, rows, name="req_77.csv"):
    p = tmp_path / name
    p.write_text(",".join(header) + "\n" + "\n".join(",".join(r) for r in rows) + "\n",
                 encoding="utf-8")
    return str(p)


def test_the_names_come_from_the_header_and_no_value_is_read(tmp_path, monkeypatch):
    _pii(monkeypatch, masked_index={"iban"})
    monkeypatch.setattr(routes_queries, "_ran_unmasked", lambda rid: False)
    path = _csv(tmp_path, ["id", "iban"], [["1", "MASKED-CELL"], ["2", SECRET]])
    row = _row(csv_file_path=path)
    names = routes_queries._status_masked_columns(row)
    assert names == ["iban"]
    texts = _texts(row, masked_columns=names)
    assert "1 PII column masked: iban." in texts
    joined = " | ".join(texts)
    assert SECRET not in joined and "MASKED-CELL" not in joined


def test_only_the_header_line_is_read():
    """A guard on the shape: the function reads one header line per statement and
    never loops over the rows of a table."""
    src = inspect.getsource(routes_queries._status_masked_columns)
    assert "next(csv.reader(fh), [])" in src
    assert "for rec in" not in src and "for row in" not in src and "readlines" not in src
    assert ".read(" not in src


def test_a_script_with_several_tables_gives_the_union_of_the_names(tmp_path, monkeypatch):
    _pii(monkeypatch, masked_index={"iban", "phone"})
    monkeypatch.setattr(routes_queries, "_ran_unmasked", lambda rid: False)
    z = tmp_path / "req_77_20261024.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("req_77_q1_a.csv", f"id,iban\n1,{SECRET}\n")
        zf.writestr("req_77_q2_a.csv", f"phone,iban\n555,{SECRET}\n")
    names = routes_queries._status_masked_columns(_row(csv_file_path=str(z)))
    assert names == ["iban", "phone"]


def test_an_unmasked_run_names_nothing(tmp_path, monkeypatch):
    _pii(monkeypatch, masked_index={"iban"})
    monkeypatch.setattr(routes_queries, "_ran_unmasked", lambda rid: True)
    path = _csv(tmp_path, ["id", "iban"], [["1", SECRET]])
    assert routes_queries._status_masked_columns(_row(csv_file_path=path)) is None


@pytest.mark.parametrize("over", [
    {"status": "failed"}, {"status": "pending"}, {"csv_file_path": None}, {"csv_file_path": "/no/such/file.csv"},
])
def test_no_stored_result_names_nothing_and_reads_nothing(tmp_path, monkeypatch, over):
    _pii(monkeypatch, masked_index={"iban"})
    asked = []
    monkeypatch.setattr(routes_queries, "_ran_unmasked", lambda rid: asked.append(rid) or False)
    base = {"csv_file_path": _csv(tmp_path, ["id", "iban"], [["1", "x"]])}
    assert routes_queries._status_masked_columns(_row(**{**base, **over})) is None
    if over.get("status") in ("failed", "pending") or "csv_file_path" in over and over["csv_file_path"] is None:
        assert asked == [], "a request with no stored result costs no audit read"


def test_a_failure_is_not_an_error_for_the_poll(tmp_path, monkeypatch, caplog):
    _pii(monkeypatch, masked_index={"iban"})
    monkeypatch.setattr(routes_queries, "_ran_unmasked", lambda rid: False)
    monkeypatch.setattr(pii, "exemption_decision", lambda *a, **k: 1 / 0)
    # `_masked_pii_cols` fails closed (dots stay on), so a catalog failure still names the column.
    path = _csv(tmp_path, ["id", "iban"], [["1", "x"]])
    assert routes_queries._status_masked_columns(_row(csv_file_path=path)) == ["iban"]
    # A file that is not a table at all gives None and a log line, never a 500.
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"not a zip")
    with caplog.at_level(logging.WARNING):
        assert routes_queries._status_masked_columns(_row(csv_file_path=str(bad))) is None
    assert any("masked columns" in r.getMessage() for r in caplog.records)


def test_pii_off_names_nothing(tmp_path, monkeypatch):
    _pii(monkeypatch, masked_index={"iban"}, enabled=False)
    monkeypatch.setattr(routes_queries, "_ran_unmasked", lambda rid: False)
    path = _csv(tmp_path, ["id", "iban"], [["1", "x"]])
    assert routes_queries._status_masked_columns(_row(csv_file_path=path)) == []
    assert mapping.masked_line([]) is None


# ---- the replica line ------------------------------------------------------------------

def _replica_row(lag):
    return _row(executed_target_id=99, run_notes={
        "statements": [{"i": 1, "leading": "select", "rows": 3}], "notices": [],
        "truncated": False, "replica": {"lag_s": lag}})


def test_a_named_replica_line():
    texts = _texts(_replica_row(2.3), replica_name="exc-orders-r1")
    assert "Ran on exc-orders-r1, about 2.3 s behind." in texts
    assert not any("read replica" in t for t in texts)


def test_a_named_replica_that_is_caught_up():
    texts = _texts(_replica_row(0.0), replica_name="exc-orders-r1")
    assert "Ran on exc-orders-r1, caught up with the primary." in texts
    assert "Ran on exc-orders-r1, caught up with the primary." == replicas.served_note(None, "exc-orders-r1")


def test_with_no_name_the_replica_line_stays_unnamed():
    texts = _texts(_replica_row(2.3))
    assert "Ran on a read replica, about 2.3 s behind the primary." in texts
    assert replicas.served_note(0.0) == "Ran on a read replica, caught up with the primary."
    assert replicas.served_note(2.3) == "Ran on a read replica, about 2.3 s behind the primary."


def test_a_run_on_the_primary_has_no_replica_line():
    assert not any("Ran on" in t for t in _texts(_row(), replica_name="exc-orders-r1"))


def test_the_replica_line_comes_before_the_success_line():
    texts = _texts(_replica_row(2.3), replica_name="exc-orders-r1", masked_columns=["iban"])
    i = texts.index("Ran on exc-orders-r1, about 2.3 s behind.")
    assert i < texts.index("184 rows in 412 ms.") < texts.index("1 PII column masked: iban.")


def test_only_a_super_admin_is_shown_the_replica_name(monkeypatch):
    monkeypatch.setattr(routes_queries, "_alias_of", lambda tid: {99: "exc-orders-r1"}.get(tid))
    asked = []
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: asked.append(uid) or uid == ME)
    row = _replica_row(2.3)
    assert routes_queries._status_replica_name(row, ME) == "exc-orders-r1"
    assert routes_queries._status_replica_name(row, "U0EXAMPLE009") is None


def test_a_chosen_replica_or_a_primary_run_has_no_name_and_costs_no_read(monkeypatch):
    """The executor writes `run_notes.replica` for an automatic route only."""
    monkeypatch.setattr(routes_queries, "_alias_of", lambda tid: "exc-orders-r1")
    asked = []
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: asked.append(uid) or True)
    chosen = _row(executed_target_id=99, run_on="replica:99",
                  run_notes={"statements": [], "notices": [], "truncated": False})
    assert routes_queries._status_replica_name(chosen, ME) is None
    assert routes_queries._status_replica_name(_row(), ME) is None
    assert routes_queries._status_replica_name(_replica_row(1.0) | {"status": "failed"}, ME) is None
    assert asked == []


def test_run_notes_stored_as_text_are_read(monkeypatch):
    monkeypatch.setattr(routes_queries, "_alias_of", lambda tid: "exc-orders-r1")
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: True)
    row = _replica_row(1.0)
    row["run_notes"] = json.dumps(row["run_notes"])
    assert routes_queries._status_replica_name(row, ME) == "exc-orders-r1"
    row["run_notes"] = "{not json"
    assert routes_queries._status_replica_name(row, ME) is None


# ---- GET /queries/{id}, end to end -------------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    logging.disable(logging.CRITICAL)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": ME, "provider": "slack", "sid": "s1", "name": "Jordan Ray"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: False)
    yield TestClient(app)
    logging.disable(logging.NOTSET)


def _get(web, monkeypatch, row, super_admin=False):
    monkeypatch.setattr(routes_queries.db, "fetch_one", lambda sql, p=None: dict(row))
    monkeypatch.setattr(routes_queries.db, "fetch_all", lambda sql, p=None: [])
    monkeypatch.setattr(routes_queries, "_alias_of", lambda tid: {7: "prod-main", 99: "exc-orders-r1"}.get(tid))
    monkeypatch.setattr(routes_queries.people, "display_name", lambda *a, **k: "Sam Ellis")
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: super_admin)
    r = web.get(f"/api/queries/{row['id']}")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_status_route_sends_the_new_lines(web, monkeypatch, tmp_path):
    _pii(monkeypatch, masked_index={"iban"})
    monkeypatch.setattr(routes_queries, "_ran_unmasked", lambda rid: False)
    path = _csv(tmp_path, ["id", "iban"], [["1", SECRET]])
    body = _get(web, monkeypatch, _row(csv_file_path=path))
    texts = [m["text"] for m in body["messages"]]
    assert texts[-2:] == ["184 rows in 412 ms.", "1 PII column masked: iban."]
    assert SECRET not in json.dumps(body), "no masked value anywhere in the status payload"
    assert body["runMs"] == 412 and body["rowCount"] == 184


def test_the_status_route_names_the_replica_for_a_super_admin_only(web, monkeypatch):
    row = _replica_row(2.3)
    named = [m["text"] for m in _get(web, monkeypatch, row, super_admin=True)["messages"]]
    assert "Ran on exc-orders-r1, about 2.3 s behind." in named
    plain = [m["text"] for m in _get(web, monkeypatch, row, super_admin=False)["messages"]]
    assert "Ran on a read replica, about 2.3 s behind the primary." in plain
    assert not any("exc-orders-r1" in t for t in plain), "a developer never sees a replica name"


def test_the_status_route_keeps_the_old_line_without_a_run_time(web, monkeypatch):
    body = _get(web, monkeypatch, _row(executed_at=None, completed_at=None))
    assert [m["text"] for m in body["messages"]][-1] == "Done — 184 row(s)."


def test_a_pending_request_costs_no_extra_read(web, monkeypatch):
    reads = []
    row = _row(status="pending", executed_at=None, completed_at=None, decided_at=None, row_count=None)
    monkeypatch.setattr(routes_queries.db, "fetch_one", lambda sql, p=None: reads.append(sql) or dict(row))
    monkeypatch.setattr(routes_queries.db, "fetch_all", lambda sql, p=None: reads.append(sql) or [])
    monkeypatch.setattr(routes_queries, "_alias_of", lambda tid: "prod-main")
    monkeypatch.setattr(routes_queries.people, "display_name", lambda *a, **k: "Sam Ellis")
    assert web.get("/api/queries/77").status_code == 200
    # The whitelist check, the request row and the audit rows, as before. The new
    # lines add nothing for a request that has not run: no unmasked check, no admin read.
    assert len(reads) == 3, reads
    assert not any("result_unmasked" in r or "FROM admins" in r for r in reads)


def test_the_web_never_prints_the_replica_in_the_slack_hint():
    """The Slack result keeps the unnamed line: `served_note` gets no name there."""
    from queryhub import executor
    assert "replicas.served_note(replica.get('lag_s'))" in inspect.getsource(executor._replica_hint)
