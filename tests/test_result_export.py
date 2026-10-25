"""Tests for the result export converters. No DB and no network."""
from __future__ import annotations

import json

import pytest

from queryhub.result_export import (
    stream_json,
    stream_markdown,
    stream_sql_insert,
)

SQL_HEAD = (
    "-- Exported from QueryHub. Empty cells are written as NULL: "
    "the stored result cannot tell NULL from an empty string.\n"
    "-- Replace table_name with the real table.\n"
)


def _text(chunks) -> str:
    return b"".join(chunks).decode("utf-8")


# (a) JSON

def test_json_exact_output():
    out = _text(stream_json(["a", "b"], [("1", "x"), ("2", "y")]))
    assert out == (
        '{"columns":["a","b"],"rows":[\n'
        '{"a":"1","b":"x"},\n'
        '{"a":"2","b":"y"}\n'
        ']}'
    )


def test_json_round_trip_special_characters():
    value = 'q"uote \\ back\nline \u0130\u0131\u015e\u015f\u011e\u011f\u00dc\u00fc\u00d6\u00f6\u00c7\u00e7'
    out = _text(stream_json(["c1", "c2"], [(value, "ok")]))
    assert json.loads(out) == {
        "columns": ["c1", "c2"],
        "rows": [{"c1": 'q"uote \\ back\nline \u0130\u0131\u015e\u015f\u011e\u011f\u00dc\u00fc\u00d6\u00f6\u00c7\u00e7', "c2": "ok"}],
    }
    assert "\u0130\u0131\u015e\u015f\u011e\u011f\u00dc\u00fc\u00d6\u00f6\u00c7\u00e7" in out


def test_json_zero_rows():
    out = _text(stream_json(["a", "b"], []))
    assert out == '{"columns":["a","b"],"rows":[\n]}'
    assert json.loads(out) == {"columns": ["a", "b"], "rows": []}


# (b) Markdown

def test_markdown_exact_output():
    rows = [("x|y", "back\\slash"), ("l1\r\nl2\nl3\rl4", "")]
    out = _text(stream_markdown(["a", "b"], rows))
    assert out == (
        "| a | b |\n"
        "| --- | --- |\n"
        "| x\\|y | back\\\\slash |\n"
        "| l1<br>l2<br>l3<br>l4 |  |\n"
    )


def test_markdown_escapes_header():
    out = _text(stream_markdown(["p|q"], [("v",)]))
    assert out == "| p\\|q |\n| --- |\n| v |\n"


def test_markdown_zero_rows():
    out = _text(stream_markdown(["a", "b"], []))
    assert out == "| a | b |\n| --- | --- |\n"


# (c) SQL

def test_sql_exact_output_with_apostrophe_and_empty():
    out = _text(stream_sql_insert(["a", "b"], [("it's", "x"), ("", "y")]))
    assert out == (
        SQL_HEAD
        + "INSERT INTO \"table_name\" (\"a\", \"b\") VALUES ('it''s', 'x');\n"
        + "INSERT INTO \"table_name\" (\"a\", \"b\") VALUES (NULL, 'y');\n"
    )


def test_sql_injection_text_stays_in_one_literal():
    out = _text(stream_sql_insert(["a", "b"], [("'); DROP TABLE x; --", "v")]))
    assert out == (
        SQL_HEAD
        + "INSERT INTO \"table_name\" (\"a\", \"b\") "
        + "VALUES ('''); DROP TABLE x; --', 'v');\n"
    )


def test_sql_identifier_with_double_quote():
    out = _text(stream_sql_insert(['we"ird'], [("1",)]))
    assert out == (
        SQL_HEAD + "INSERT INTO \"table_name\" (\"we\"\"ird\") VALUES ('1');\n"
    )


def test_sql_nul_removed_and_newline_kept():
    out = _text(stream_sql_insert(["a"], [("x\x00y\nz",)]))
    assert out == SQL_HEAD + "INSERT INTO \"table_name\" (\"a\") VALUES ('xy\nz');\n"


def test_sql_schema_qualified_table():
    out = _text(stream_sql_insert(["a"], [("1",)], table="s.t"))
    assert out == SQL_HEAD + "INSERT INTO \"s\".\"t\" (\"a\") VALUES ('1');\n"


def test_sql_zero_rows_yields_only_comments():
    assert _text(stream_sql_insert(["a"], [])) == SQL_HEAD


@pytest.mark.parametrize("bad", ['"x"; --', "1abc", "", "a.b.c", "a b"])
def test_sql_invalid_table_name(bad):
    with pytest.raises(ValueError):
        list(stream_sql_insert(["a"], [("1",)], table=bad))


# (d) padding and cutting

def test_json_pads_and_cuts():
    out = _text(stream_json(["a", "b"], [("1",), ("1", "2", "3")]))
    assert json.loads(out)["rows"] == [{"a": "1", "b": ""}, {"a": "1", "b": "2"}]


def test_markdown_pads_and_cuts():
    out = _text(stream_markdown(["a", "b"], [("1",), ("1", "2", "3")]))
    assert out == "| a | b |\n| --- | --- |\n| 1 |  |\n| 1 | 2 |\n"


def test_sql_pads_and_cuts():
    out = _text(stream_sql_insert(["a", "b"], [("1",), ("1", "2", "3")]))
    assert out == (
        SQL_HEAD
        + "INSERT INTO \"table_name\" (\"a\", \"b\") VALUES ('1', NULL);\n"
        + "INSERT INTO \"table_name\" (\"a\", \"b\") VALUES ('1', '2');\n"
    )


# (e) empty columns

@pytest.mark.parametrize("func", [stream_json, stream_markdown, stream_sql_insert])
def test_empty_columns_raises(func):
    with pytest.raises(ValueError):
        next(func([], [("1",)]))


# (f) laziness

class _CountingRows:
    def __init__(self, total: int):
        self.total = total
        self.handed_out = 0

    def __iter__(self):
        for i in range(self.total):
            self.handed_out += 1
            yield (str(i), "v")


@pytest.mark.parametrize("func", [stream_json, stream_sql_insert])
def test_first_chunk_does_not_read_all_rows(func):
    source = _CountingRows(10)
    gen = func(["a", "b"], source)
    first = next(gen)
    assert isinstance(first, bytes)
    assert source.handed_out <= 2
    assert source.handed_out < source.total
