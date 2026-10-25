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


def stream_json(columns: list[str], rows: Iterable[Sequence[str]]) -> Iterator[bytes]:
    """Yield a JSON document: the column list and one object per row."""
    _check_columns(columns)
    names = [json.dumps(c, ensure_ascii=False) for c in columns]
    head = json.dumps(columns, ensure_ascii=False, separators=(",", ":"))
    yield ('{"columns":' + head + ',"rows":[\n').encode("utf-8")
    first = True
    for cells in _fit(rows, len(columns)):
        pairs = [n + ":" + json.dumps(v, ensure_ascii=False) for n, v in zip(names, cells)]
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
