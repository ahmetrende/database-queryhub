"""JSON values from typed columns (2026-11-11).

The stored result is a CSV file: every cell is text and a NULL is ''. With the
column types that the executor records, the JSON download gives numbers,
booleans and nulls. A text column keeps '' as "", because there an empty string
and NULL look the same. The web grid's Copy as JSON uses the same rule
(QueryHubWeb/app/test/result-json-types.test.mjs holds the same cases).
"""
import asyncio
import json

import pytest

from queryhub import result_export
from queryhub.web import routes_queries

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Example"}

KINDS = [
    ("int4", "num"), ("int8", "num"), ("numeric(10,2)", "num"), ("float8", "num"),
    ("double precision", "num"), ("bigint", "num"), ("decimal(18, 2)", "num"),
    ("Nullable(Int64)", "num"), ("UInt32", "num"), ("Float64", "num"),
    ("LowCardinality(Nullable(String))", "text"), ("bool", "bool"), ("boolean", "bool"),
    ("bit", "bool"), ("text", "text"), ("varchar(20)", "text"),
    ("character varying", "text"), ("nvarchar", "text"), ("String", "text"),
    ("date", "other"), ("timestamptz", "other"), ("uuid", "other"), ("jsonb", "other"),
    ("int4[]", "other"), ("Array(String)", "other"), ("", "text"), (None, "text"),
]


@pytest.mark.parametrize("sql_type,kind", KINDS)
def test_kind_of_a_type(sql_type, kind):
    assert result_export.json_kind(sql_type) == kind


@pytest.mark.parametrize("value,kind,out", [
    ("12", "num", "12"), ("-0.5", "num", "-0.5"), ("1e5", "num", "1e5"),
    ("9007199254740993", "num", "9007199254740993"),
    ("", "num", "null"), ("1***", "num", '"1***"'), ("NaN", "num", '"NaN"'),
    ("007", "num", '"007"'),
    ("t", "bool", "true"), ("False", "bool", "false"), ("1", "bool", "true"),
    ("", "bool", "null"), ("maybe", "bool", '"maybe"'),
    ("", "text", '""'), ("12", "text", '"12"'),
    ("", "other", "null"), ("2026-01-01", "other", '"2026-01-01"'),
])
def test_cell(value, kind, out):
    assert result_export.json_cell(value, kind) == out


def test_stream_with_types():
    types = {"id": "int4", "amount": "numeric(10,2)", "ok": "bool", "d": "date", "name": "text"}
    rows = [["1", "10.50", "t", "2026-01-01", "a"], ["2", "", "", "", ""]]
    text = b"".join(result_export.stream_json(list(types), rows, types)).decode()
    assert text == (
        '{"columns":["id","amount","ok","d","name"],"rows":[\n'
        '{"id":1,"amount":10.50,"ok":true,"d":"2026-01-01","name":"a"},\n'
        '{"id":2,"amount":null,"ok":null,"d":null,"name":""}\n]}')
    assert json.loads(text)["rows"][1]["amount"] is None


def test_stream_without_types_keeps_strings():
    text = b"".join(result_export.stream_json(["a"], [["1"], [""]])).decode()
    assert json.loads(text)["rows"] == [{"a": "1"}, {"a": ""}]


def _body(resp) -> bytes:
    async def collect():
        return b"".join([c async for c in resp.body_iterator])
    return asyncio.run(collect())


@pytest.fixture
def stored(tmp_path, monkeypatch):
    p = tmp_path / "req_42_20260925T070000Z.csv"
    p.write_bytes("id,name,big\n1,a,9007199254740993\n2,,\n".encode("utf-8"))
    state = {"row": {"status": "completed", "csv_file_path": str(p)}}
    monkeypatch.setattr(routes_queries.deps, "require_whitelisted", lambda c: None)
    monkeypatch.setattr(routes_queries, "_own_request", lambda rid, uid: state["row"])
    monkeypatch.setattr(routes_queries.audit_mod, "log", lambda *a, **k: None)
    return state


@pytest.mark.parametrize("types", [
    {"id": "int4", "name": "text", "big": "int8"},
    json.dumps({"id": "int4", "name": "text", "big": "int8"}),
])
def test_download_uses_the_recorded_types(stored, types):
    stored["row"]["result_column_types"] = types
    body = _body(routes_queries.query_result_json(42, None, claims=CLAIMS)).decode()
    assert '{"id":1,"name":"a","big":9007199254740993}' in body
    assert '{"id":2,"name":"","big":null}' in body


@pytest.mark.parametrize("types", [None, "", "not json", {}])
def test_download_without_types_keeps_strings(stored, types):
    stored["row"]["result_column_types"] = types
    body = _body(routes_queries.query_result_json(42, None, claims=CLAIMS)).decode()
    assert '{"id":"1","name":"a","big":"9007199254740993"}' in body
