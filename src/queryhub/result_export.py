"""Turn rows of a stored result into JSON, a Markdown table or SQL INSERTs.

The stored result is a CSV that is already PII-masked. These converters only
change the shape. They never see unmasked data.

Each converter is a generator. It yields small UTF-8 chunks, about one row per
chunk. A 100 MB result must never sit in memory, so no code here builds a list
from the incoming rows.

The stored CSV cannot tell NULL from an empty string. So JSON keeps every value
as a string. SQL writes an empty cell as NULL. The SQL output says so in its
first comment line.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Sequence

_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

_SQL_HEADER = (
    "-- Exported from QueryHub. Empty cells are written as NULL: "
    "the stored result cannot tell NULL from an empty string.\n"
    "-- Replace table_name with the real table.\n"
)


def _check_columns(columns: list[str]) -> None:
    if not columns:
        raise ValueError("columns must not be empty")


def _fit(rows: Iterable[Sequence[str]], width: int) -> Iterator[list[str]]:
    """Yield each row padded with "" or cut to exactly `width` cells."""
    for row in rows:
        cells = list(row[:width])
        if len(cells) < width:
            cells.extend([""] * (width - len(cells)))
        yield cells


# JSON values from typed columns (2026-11-11). The stored result is a CSV
# file, so every cell is text and a NULL is ''. With the column's SQL type
# (requests.result_column_types) a cell becomes a JSON value: a number column
# gives a JSON number (the text as it is, so a bigint keeps every digit), a
# boolean column gives true or false, and an empty cell of a column that is not
# text gives null. A text column keeps '' as "": there it can be an empty
# string or NULL, and the file does not say which. A masked or odd value that
# does not read as a number or a boolean stays a string. The web grid's
# "Copy as JSON" uses the same rule (qhResJsonKind in qh-results-tools.jsx).
_JSON_NUM_TYPES = re.compile(
    r"^(u?int(2|4|8|16|32|64|128|256)?|integer|smallint|bigint|tinyint|mediumint"
    r"|serial|bigserial|smallserial|numeric|decimal|real|float(4|8|32|64)?"
    r"|double( precision)?|oid)$")
_JSON_BOOL_TYPES = {"bool", "boolean", "bit"}
_JSON_TEXT_TYPES = re.compile(
    r"^(text|varchar|char|character( varying)?|nvarchar|nchar|ntext|bpchar|name"
    r"|citext|string|fixedstring|str|sysname|xml|enum(8|16)?)$")
_JSON_NUMBER = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$")


def json_kind(sql_type) -> str:
    """'num', 'bool', 'text' or 'other' for a column's SQL type. No type is 'text'."""
    t = str(sql_type or "").strip().lower()
    unwrapped = True
    while unwrapped:                      # LowCardinality(Nullable(String))
        unwrapped = False
        for wrap in ("nullable(", "lowcardinality("):
            if t.startswith(wrap) and t.endswith(")"):
                t, unwrapped = t[len(wrap):-1].strip(), True
    if not t:
        return "text"
    if t.endswith("[]") or t.startswith("array("):
        return "other"
    base = t.split("(", 1)[0].strip()
    if _JSON_NUM_TYPES.match(base):
        return "num"
    if base in _JSON_BOOL_TYPES:
        return "bool"
    if _JSON_TEXT_TYPES.match(base):
        return "text"
    return "other"


def json_cell(value: str, kind: str) -> str:
    """The JSON text of one CSV cell of a column of `kind` (see json_kind)."""
    if kind == "text":
        return json.dumps(value, ensure_ascii=False)
    if value == "":
        return "null"
    if kind == "num" and _JSON_NUMBER.match(value):
        return value
    if kind == "bool":
        low = value.strip().lower()
        if low in ("t", "true", "1"):
            return "true"
        if low in ("f", "false", "0"):
            return "false"
    return json.dumps(value, ensure_ascii=False)


def stream_json(columns: list[str], rows: Iterable[Sequence[str]],
                types: dict | None = None) -> Iterator[bytes]:
    """Yield a JSON document: the column list and one object per row.

    `types` maps a column name to its SQL type. Without it, every value is a
    string, as before."""
    _check_columns(columns)
    names = [json.dumps(c, ensure_ascii=False) for c in columns]
    kinds = [json_kind((types or {}).get(c)) for c in columns]
    head = json.dumps(columns, ensure_ascii=False, separators=(",", ":"))
    yield ('{"columns":' + head + ',"rows":[\n').encode("utf-8")
    first = True
    for cells in _fit(rows, len(columns)):
        pairs = [n + ":" + json_cell(v, k) for n, v, k in zip(names, cells, kinds)]
        piece = "{" + ",".join(pairs) + "}"
        if not first:
            piece = ",\n" + piece
        first = False
        yield piece.encode("utf-8")
    yield (b"]}" if first else b"\n]}")


def _md_cell(value: str) -> str:
    value = value.replace("\\", "\\\\").replace("|", "\\|")
    return value.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def _md_line(cells: Sequence[str]) -> bytes:
    return ("| " + " | ".join(_md_cell(c) for c in cells) + " |\n").encode("utf-8")


def stream_markdown(columns: list[str], rows: Iterable[Sequence[str]]) -> Iterator[bytes]:
    """Yield a GitHub-flavoured Markdown table."""
    _check_columns(columns)
    yield _md_line(columns)
    yield ("| " + " | ".join(["---"] * len(columns)) + " |\n").encode("utf-8")
    for cells in _fit(rows, len(columns)):
        yield _md_line(cells)


def _sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sql_value(value: str) -> str:
    value = value.replace("\x00", "")
    if value == "":
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def stream_sql_insert(
    columns: list[str],
    rows: Iterable[Sequence[str]],
    table: str = "table_name",
) -> Iterator[bytes]:
    """Yield two comment lines, then one INSERT statement per row."""
    _check_columns(columns)
    if not _TABLE_RE.fullmatch(table):
        raise ValueError("invalid table name")
    target = ".".join(_sql_ident(part) for part in table.split("."))
    col_list = ", ".join(_sql_ident(c) for c in columns)
    prefix = "INSERT INTO " + target + " (" + col_list + ") VALUES ("
    for line in _SQL_HEADER.splitlines(keepends=True):
        yield line.encode("utf-8")
    for cells in _fit(rows, len(columns)):
        values = ", ".join(_sql_value(v) for v in cells)
        yield (prefix + values + ");\n").encode("utf-8")
