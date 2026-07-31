"""CSV bulk-import helpers: permission, parsing, validation, naming.

The import pipeline is separate from the SQL query path. A user with an
import grant uploads a CSV via `/sql import`; the bot parses + validates
it here, an admin approves, and executor.import_run() COPYs it into the
`dba` schema (new or existing table). Schema is ALWAYS 'dba' — enforced
here and in the executor.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from . import config as cfg

# Fixed schema for every import. The feature can never write outside it.
IMPORT_SCHEMA = "dba"

# Allowed CSV delimiters, keyed by the modal radio value.
DELIMITERS = {
    "comma":     ",",
    "semicolon": ";",
    "tab":       "\t",
}


def is_enabled() -> bool:
    return (cfg.get_setting("csv_import_enabled", "off") or "").strip().lower() in {
        "on", "true", "yes", "1",
    }


def can_import(principal_id: str) -> bool:
    """Bot-level import permission. Admins always pass (same bypass as the
    RW/DDL grant path); everyone else must be in the import_grants
    allowlist."""
    from . import admins, db
    if admins.is_admin(principal_id):
        return True
    row = db.fetch_one(
        "SELECT 1 FROM import_grants WHERE slack_user_id = %s", (principal_id,)
    )
    return row is not None


# --- column-name normalization --------------------------------------------

_IDENT_BAD = re.compile(r"[^a-z0-9_]+")


def normalize_column(raw: str, index: int, seen: set[str]) -> str:
    """Turn a CSV header cell into a safe, unique lowercase identifier.
    'User Name' -> 'user_name', 'Σ total' -> 'total', '' -> 'col_3'.
    Leading digit gets a 'c_' prefix; collisions get a numeric suffix."""
    s = (raw or "").strip().lower()
    s = _IDENT_BAD.sub("_", s).strip("_")
    if not s:
        s = f"col_{index + 1}"
    if s[0].isdigit():
        s = "c_" + s
    s = s[:63]  # Postgres identifier limit
    base, n = s, 1
    while s in seen:
        suffix = f"_{n}"
        s = base[: 63 - len(suffix)] + suffix
        n += 1
    seen.add(s)
    return s


# Allow-listed column types for user-supplied schemas. Anything outside
# this set is rejected — the user gives "name type" pairs, never raw SQL,
# so there's no CREATE TABLE injection surface. Base type + optional
# (n) / (n,m) precision is permitted.
_ALLOWED_TYPES = frozenset({
    "text", "varchar", "char", "character varying", "character",
    "int", "integer", "int4", "bigint", "int8", "smallint", "int2",
    "numeric", "decimal", "real", "float4", "double precision", "float8",
    "money", "boolean", "bool",
    "date", "timestamp", "timestamptz", "timestamp with time zone",
    "timestamp without time zone", "time", "timetz", "interval",
    "uuid", "json", "jsonb", "bytea", "inet", "cidr", "macaddr",
})

_TYPE_RE = re.compile(r"^([a-z][a-z ]*?)\s*(\(\s*\d+\s*(?:,\s*\d+\s*)?\))?$")


def parse_column_defs(text: str, expected_count: int) -> tuple[list[dict] | None, str | None]:
    """Parse user-supplied 'name type, name type, ...' into validated
    [{'name':..,'type':..}] pairs. Column names are normalized to safe
    identifiers; types must be in the allow-list (no raw SQL). Returns
    (defs, None) on success or (None, error). `expected_count` is the
    CSV's column count — the schema must match it exactly (COPY maps by
    position)."""
    # Split on top-level commas only — so numeric(10,2)'s inner comma
    # doesn't break a column apart.
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1; cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1); cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return None, "No column definitions found."
    seen: set[str] = set()
    defs: list[dict] = []
    for p in parts:
        m = re.match(r"^(\S+)\s+(.+)$", p)
        if not m:
            return None, f"Bad column definition: `{p}` (expected `name type`)."
        name = normalize_column(m.group(1), len(defs), seen)
        type_raw = m.group(2).strip().lower()
        tm = _TYPE_RE.match(type_raw)
        if not tm:
            return None, f"Invalid type in `{p}`."
        base = tm.group(1).strip()
        if base not in _ALLOWED_TYPES:
            return None, (f"Type `{base}` is not allowed. Use a standard "
                          f"Postgres type (text, int, numeric, timestamptz, …).")
        safe_type = base + (tm.group(2).replace(" ", "") if tm.group(2) else "")
        defs.append({"name": name, "type": safe_type})
    if len(defs) != expected_count:
        return None, (f"You defined {len(defs)} column(s) but the CSV has "
                      f"{expected_count}. They must match (COPY maps by position).")
    return defs, None


def create_table_preview(table: str, unlogged: bool,
                         columns: list[str], column_defs: list[dict] | None) -> str:
    """Human-readable CREATE TABLE the import will run, for the admin DM.
    Mirrors what executor._import_run builds (col_defs win over all-TEXT)."""
    kw = "UNLOGGED " if unlogged else ""
    if column_defs:
        lines = [f'  "{d["name"]}" {d["type"]}' for d in column_defs]
    else:
        lines = [f'  "{c}" text' for c in columns]
    return f'CREATE {kw}TABLE {IMPORT_SCHEMA}."{table}" (\n' + ",\n".join(lines) + "\n);"


@dataclass
class ParsedCsv:
    columns: list[str]            # normalized identifiers
    raw_header: list[str]         # original header cells
    row_count: int                # data rows (excludes header)
    byte_size: int
    sample_rows: list[list[str]]  # first few data rows (for the admin preview)
    error: str | None = None      # set when parsing failed a hard check


def parse_csv(data: bytes, delimiter: str, *, sample: int = 3) -> ParsedCsv:
    """Parse + validate raw CSV bytes. Enforces UTF-8, a header row, the
    row-count and size caps. Returns ParsedCsv with .error set (and other
    fields best-effort) when a hard check fails."""
    byte_size = len(data)
    max_mb = cfg.get_int("import_max_mb", 50)
    if byte_size > max_mb * 1024 * 1024:
        return ParsedCsv([], [], 0, byte_size, [],
                         error=f"CSV is {byte_size // 1024 // 1024} MB, over the "
                               f"{max_mb} MB limit (bot_config.import_max_mb).")
    try:
        text = data.decode("utf-8-sig")  # tolerate a BOM, reject other encodings
    except UnicodeDecodeError:
        return ParsedCsv([], [], 0, byte_size, [],
                         error="CSV is not valid UTF-8. Re-save it as UTF-8 "
                               "(in Excel: 'CSV UTF-8').")

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return ParsedCsv([], [], 0, byte_size, [], error="CSV is empty.")
    if not header or all(not (c or "").strip() for c in header):
        return ParsedCsv([], [], 0, byte_size, [],
                         error="CSV has no header row (the first line must be "
                               "column names).")

    seen: set[str] = set()
    columns = [normalize_column(c, i, seen) for i, c in enumerate(header)]

    max_rows = cfg.get_int("import_max_rows", 100000)
    sample_rows: list[list[str]] = []
    row_count = 0
    for row in reader:
        if not row:  # skip blank lines
            continue
        row_count += 1
        if len(sample_rows) < sample:
            sample_rows.append(row)
        if row_count > max_rows:
            return ParsedCsv(columns, list(header), row_count, byte_size, sample_rows,
                             error=f"CSV exceeds the {max_rows:,}-row limit "
                                   f"(bot_config.import_max_rows).")
    if row_count == 0:
        return ParsedCsv(columns, list(header), 0, byte_size, [],
                         error="CSV has a header but no data rows.")

    return ParsedCsv(columns, list(header), row_count, byte_size, sample_rows)
