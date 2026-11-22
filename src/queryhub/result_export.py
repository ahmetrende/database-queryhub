"""Turn rows of a stored result into JSON, a Markdown table or SQL INSERTs.

The stored result is a CSV that is already PII-masked. These converters only
change the shape. They never see unmasked data.

Each converter is a generator. It yields small UTF-8 chunks, about one row per
chunk. A 100 MB result must never sit in memory, so no code here builds a list
from the incoming rows.

The stored CSV cannot tell NULL from an empty string. The executor can keep a
NULL sidecar next to the CSV (`<name>.csv.nulls`). With it, JSON and SQL write
NULL only for real NULLs and keep an empty string as ''. Without it, SQL writes
an empty cell as NULL and says so in its first comment line.
"""
from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

_SQL_HEADER = (
    "-- Exported from QueryHub. Empty cells are written as NULL: "
    "the stored result cannot tell NULL from an empty string.\n"
    "-- Replace table_name with the real table.\n"
)
_SQL_HEADER_EXACT = (
    "-- Exported from QueryHub. NULL and empty strings are kept apart.\n"
    "-- Replace table_name with the real table.\n"
)

# Same name and cap as the executor's writer (executor.NULLS_SUFFIX).
NULLS_SUFFIX = ".nulls"
_NULLS_MAX_READ = 20 * 1024 * 1024

NullMask = dict[int, set[int]]


def null_mask(csv_path: Path) -> NullMask | None:
    """The NULL cells of a stored CSV, from its sidecar: {row: {col, ...}}.

    Rows and columns are 0-based data positions. None means "do not know":
    there is no sidecar, it is too big, it does not parse, or its column
    count does not match the CSV header. The caller then keeps the old
    behaviour."""
    side = csv_path.with_name(csv_path.name + NULLS_SUFFIX)
    try:
        if not side.is_file() or side.stat().st_size > _NULLS_MAX_READ:
            return None
        with csv_path.open(newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), None)
        lines = side.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    if header is None or not lines:
        return None
    head = lines[0].split(" ")
    if len(head) != 2 or head[0] != "v1" or not head[1].isdigit():
        return None
    ncols = int(head[1])
    if ncols != len(header):
        return None
    mask: NullMask = {}
    for line in lines[1:]:
        parts = line.split(" ")
        if len(parts) != 2 or not parts[0].isdigit():
            return None
        cols = parts[1].split(",")
        if not all(c.isdigit() for c in cols):
            return None
        idx = {int(c) for c in cols}
        if max(idx) >= ncols:
            return None
        mask.setdefault(int(parts[0]), set()).update(idx)
    return mask


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


def _json_exact(value: str, kind: str, is_null: bool) -> str:
    """One cell when the NULL mask is known: null only for a real NULL."""
    if is_null:
        return "null"
    if value == "":
        return '""'
    return json_cell(value, kind)


def stream_json(columns: list[str], rows: Iterable[Sequence[str]],
                types: dict | None = None,
                nulls: NullMask | None = None) -> Iterator[bytes]:
    """Yield a JSON document: the column list and one object per row.

    `types` maps a column name to its SQL type. Without it, every value is a
    string, as before. `nulls` is the NULL mask (null_mask). With it, a real
    NULL is null and an empty string is "". Without it, nothing changes."""
    _check_columns(columns)
    names = [json.dumps(c, ensure_ascii=False) for c in columns]
    kinds = [json_kind((types or {}).get(c)) for c in columns]
    head = json.dumps(columns, ensure_ascii=False, separators=(",", ":"))
    yield ('{"columns":' + head + ',"rows":[\n').encode("utf-8")
    first = True
    for row_no, cells in enumerate(_fit(rows, len(columns))):
        if nulls is None:
            pairs = [n + ":" + json_cell(v, k)
                     for n, v, k in zip(names, cells, kinds)]
        else:
            row_nulls = nulls.get(row_no, ())
            pairs = [n + ":" + _json_exact(v, k, i in row_nulls)
                     for i, (n, v, k) in enumerate(zip(names, cells, kinds))]
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


def _sql_exact(value: str, is_null: bool) -> str:
    """One value when the NULL mask is known: NULL only for a real NULL."""
    if is_null:
        return "NULL"
    return "'" + value.replace("\x00", "").replace("'", "''") + "'"


def stream_sql_insert(
    columns: list[str],
    rows: Iterable[Sequence[str]],
    table: str = "table_name",
    nulls: NullMask | None = None,
) -> Iterator[bytes]:
    """Yield two comment lines, then one INSERT statement per row.

    `nulls` is the NULL mask (null_mask). With it, only a real NULL is NULL
    and an empty string is ''. Without it, every empty cell is NULL."""
    _check_columns(columns)
    if not _TABLE_RE.fullmatch(table):
        raise ValueError("invalid table name")
    target = ".".join(_sql_ident(part) for part in table.split("."))
    col_list = ", ".join(_sql_ident(c) for c in columns)
    prefix = "INSERT INTO " + target + " (" + col_list + ") VALUES ("
    header = _SQL_HEADER if nulls is None else _SQL_HEADER_EXACT
    for line in header.splitlines(keepends=True):
        yield line.encode("utf-8")
    for row_no, cells in enumerate(_fit(rows, len(columns))):
        if nulls is None:
            values = ", ".join(_sql_value(v) for v in cells)
        else:
            row_nulls = nulls.get(row_no, ())
            values = ", ".join(_sql_exact(v, i in row_nulls)
                               for i, v in enumerate(cells))
        yield (prefix + values + ");\n").encode("utf-8")
