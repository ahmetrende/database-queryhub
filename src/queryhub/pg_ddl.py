"""The CREATE script of one PostgreSQL object (2026-11-12, Devart B5).

A view, a materialized view, a function and a procedure come from the
server's own functions (pg_get_viewdef, pg_get_functiondef). A table has no
stored text, so `object_ddl` builds it from the catalog: the columns with
their types, defaults, identity and NOT NULL, the constraints, the partition
key, the partitions, the indexes that no constraint owns, the comments and
the table grants. It leaves out storage options, column grants, triggers and
policies.

A partitioned table lists its partitions after it, sub-partitions included,
up to `PARTITION_CAP`. A table that is a partition shows as
`PARTITION OF <parent> FOR VALUES ...` with its own local constraints only.

Every read takes a cursor of a READ ONLY transaction, which the caller opens.
"""
from __future__ import annotations

import re

PARTITION_CAP = 100

_REL_SQL = (
    "SELECT c.oid, c.relkind, n.nspname, c.relname, c.relispartition "
    "  FROM pg_catalog.pg_class c "
    "  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    " WHERE c.oid = pg_catalog.to_regclass(%s)")
_COLS_SQL = (
    "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull, "
    "       pg_catalog.pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated, "
    "       pg_catalog.col_description(a.attrelid, a.attnum) "
    "  FROM pg_catalog.pg_attribute a "
    "  LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
    " WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped "
    " ORDER BY a.attnum")
_CONS_SQL = (
    "SELECT conname, pg_catalog.pg_get_constraintdef(oid, true), conislocal "
    "  FROM pg_catalog.pg_constraint "
    " WHERE conrelid = %s AND contype IN ('p', 'u', 'c', 'f', 'x') "
    " ORDER BY contype <> 'p', conname")
# An index that is a partition of a parent index comes from the parent's
# CREATE INDEX, so it is not shown.
_INDEX_SQL = (
    "SELECT pg_catalog.pg_get_indexdef(i.indexrelid) "
    "  FROM pg_catalog.pg_index i "
    " WHERE i.indrelid = %s "
    "   AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conindid = i.indexrelid) "
    "   AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits h WHERE h.inhrelid = i.indexrelid) "
    " ORDER BY 1")
# The parent and the bound of a table that is a partition.
_PARENT_SQL = (
    "SELECT pn.nspname, p.relname, pg_catalog.pg_get_expr(c.relpartbound, c.oid) "
    "  FROM pg_catalog.pg_class c "
    "  JOIN pg_catalog.pg_inherits h ON h.inhrelid = c.oid "
    "  JOIN pg_catalog.pg_class p ON p.oid = h.inhparent "
    "  JOIN pg_catalog.pg_namespace pn ON pn.oid = p.relnamespace "
    " WHERE c.oid = %s")
# All partitions under a partitioned table, depth first, so that a
# sub-partition comes after its parent. `total` counts the rows before LIMIT.
_PARTS_SQL = (
    "WITH RECURSIVE tree AS ( "
    "    SELECT h.inhrelid AS oid, h.inhparent AS parent, ARRAY[c.relname::text] AS path "
    "      FROM pg_catalog.pg_inherits h "
    "      JOIN pg_catalog.pg_class c ON c.oid = h.inhrelid "
    "     WHERE h.inhparent = %s AND c.relispartition "
    "    UNION ALL "
    "    SELECT h.inhrelid, h.inhparent, t.path || c.relname::text "
    "      FROM tree t "
    "      JOIN pg_catalog.pg_inherits h ON h.inhparent = t.oid "
    "      JOIN pg_catalog.pg_class c ON c.oid = h.inhrelid "
    "     WHERE c.relispartition) "
    "SELECT n.nspname, c.relname, pn.nspname, p.relname, "
    "       pg_catalog.pg_get_expr(c.relpartbound, c.oid), "
    "       CASE WHEN c.relkind = 'p' THEN pg_catalog.pg_get_partkeydef(c.oid) END, "
    "       count(*) OVER () AS total "
    "  FROM tree t "
    "  JOIN pg_catalog.pg_class c ON c.oid = t.oid "
    "  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "  JOIN pg_catalog.pg_class p ON p.oid = t.parent "
    "  JOIN pg_catalog.pg_namespace pn ON pn.oid = p.relnamespace "
    " ORDER BY t.path "
    " LIMIT %s")
# Table grants, one row per grantee and privilege. A NULL relacl gives no
# rows: only the owner has rights. The owner's own grants are not shown.
_GRANTS_SQL = (
    "SELECT a.grantee = 0, r.rolname, a.privilege_type, a.is_grantable "
    "  FROM pg_catalog.pg_class c "
    " CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a "
    "  LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee "
    " WHERE c.oid = %s AND a.grantee <> c.relowner")
_PRIV_ORDER = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN")
_FUNC_SQL = (
    "SELECT p.oid, p.prokind "
    "  FROM pg_catalog.pg_proc p "
    "  JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
    " WHERE p.proname = %s AND p.prokind IN ('f', 'p', 'w') "
    "   AND (n.nspname = %s OR (%s::text IS NULL AND pg_catalog.pg_function_is_visible(p.oid))) "
    " ORDER BY p.oid LIMIT 20")
_RELKINDS = {"r": "table", "p": "table", "f": "table", "v": "view", "m": "materialized view"}


def quote_ident(name: str) -> str:
    """A name as PostgreSQL writes it: bare when it is lower case letters,
    digits and _ (not first a digit), else in double quotes."""
    if name and name[0].isalpha() and name.islower() and all(ch.isalnum() or ch == "_" for ch in name):
        return name
    return '"' + name.replace('"', '""') + '"'


def quote_literal(text: str) -> str:
    """A text as a SQL string literal: in single quotes, each quote doubled."""
    return "'" + text.replace("'", "''") + "'"


def name_parts(raw: str) -> list[str] | None:
    """The parts of a dotted name as PostgreSQL reads them: a bare part in
    lower case, a "quoted" part as written. None when the name does not parse."""
    parts, i, n = [], 0, len(raw)
    while True:
        while i < n and raw[i].isspace():
            i += 1
        if i < n and raw[i] == '"':
            j, buf = i + 1, []
            while j < n:
                if raw[j] == '"':
                    if j + 1 < n and raw[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(raw[j])
                j += 1
            if j >= n:
                return None
            part, i = "".join(buf), j + 1
        else:
            j = i
            while j < n and raw[j] not in '."' and not raw[j].isspace():
                j += 1
            part, i = raw[i:j].lower(), j
        if not part:
            return None
        parts.append(part)
        while i < n and raw[i].isspace():
            i += 1
        if i == n:
            return parts
        if raw[i] != ".":
            return None
        i += 1


def object_ddl(cur, schema: str | None, name: str) -> tuple[str, str, bool] | None:
    """(kind, script, built) of `schema.name` (or `name` on the search path),
    or None when there is no such relation and no such function. The parts
    come from `name_parts`: folded and unquoted."""
    ref = (quote_ident(schema) + "." if schema else "") + quote_ident(name)
    cur.execute(_REL_SQL, (ref,))
    row = cur.fetchone()
    if row:
        oid, relkind, nsp, rel, is_part = row
        kind = _RELKINDS.get(relkind)
        if kind is None:
            return None
        full = quote_ident(nsp) + "." + quote_ident(rel)
        if kind in ("view", "materialized view"):
            cur.execute("SELECT pg_catalog.pg_get_viewdef(%s, true)", (oid,))
            body = (cur.fetchone()[0] or "").strip()
            head = "CREATE MATERIALIZED VIEW " if kind == "materialized view" else "CREATE OR REPLACE VIEW "
            return (kind, head + full + " AS\n" + body, False)
        return ("table", _table_script(cur, oid, relkind, full, bool(is_part)), True)
    cur.execute(_FUNC_SQL, (name, schema, schema))
    procs = cur.fetchall()
    if not procs:
        return None
    parts = []
    for oid, _kind in procs:
        cur.execute("SELECT pg_catalog.pg_get_functiondef(%s)", (oid,))
        parts.append((cur.fetchone()[0] or "").strip() + ";")
    kind = "procedure" if all(k == "p" for _o, k in procs) else "function"
    return (kind, "\n\n".join(parts), False)


def _table_script(cur, oid, relkind: str, full: str, is_part: bool = False) -> str:
    cur.execute(_COLS_SQL, (oid,))
    lines, col_notes = [], []
    for col, typ, notnull, default, identity, generated, note in cur.fetchall():
        line = f"    {quote_ident(col)} {typ}"
        if generated == "s" and default:
            line += f" GENERATED ALWAYS AS ({default}) STORED"
        elif identity in ("a", "d"):
            line += " GENERATED " + ("ALWAYS" if identity == "a" else "BY DEFAULT") + " AS IDENTITY"
        elif default:
            line += f" DEFAULT {default}"
        if notnull:
            line += " NOT NULL"
        lines.append(line)
        if note is not None:
            col_notes.append((col, note))
    cur.execute(_CONS_SQL, (oid,))
    cons = cur.fetchall()
    parent = None
    if is_part:
        cur.execute(_PARENT_SQL, (oid,))
        parent = cur.fetchone()
    if parent:
        # The columns and the inherited constraints come from the parent.
        pnsp, prel, bound = parent
        local = [f"    CONSTRAINT {quote_ident(c)} {d}" for c, d, is_local in cons if is_local]
        script = "CREATE TABLE " + full + " PARTITION OF " + quote_ident(pnsp) + "." + quote_ident(prel)
        if local:
            script += " (\n" + ",\n".join(local) + "\n)"
        script += " " + (bound or "DEFAULT")
    else:
        lines += [f"    CONSTRAINT {quote_ident(c)} {d}" for c, d, _local in cons]
        head = "CREATE FOREIGN TABLE " if relkind == "f" else "CREATE TABLE "
        script = head + full + " (\n" + ",\n".join(lines) + "\n)"
    if relkind == "p":
        cur.execute("SELECT pg_catalog.pg_get_partkeydef(%s)", (oid,))
        key = cur.fetchone()[0]
        if key:
            script += " PARTITION BY " + key
    script += ";"
    if relkind == "p":
        script += _partitions(cur, oid)
    cur.execute(_INDEX_SQL, (oid,))
    for (idx,) in cur.fetchall():
        # The server writes the index of a partitioned table as ON ONLY. The
        # script creates the partitions first, so ON ONLY would leave them
        # without the index and the parent index invalid. ON builds it on
        # every partition.
        if relkind == "p":
            idx = re.sub(r" ON ONLY ", " ON ", idx, count=1)
        script += "\n" + idx + ";"
    script += _comments(cur, oid, full, col_notes)
    script += _grants(cur, oid, full)
    return script


def _partitions(cur, oid) -> str:
    """The CREATE TABLE ... PARTITION OF lines under a partitioned table,
    at most PARTITION_CAP, then one comment line with the rest."""
    cur.execute(_PARTS_SQL, (oid, PARTITION_CAP))
    rows = cur.fetchall()
    out, total = "", 0
    for nsp, rel, pnsp, prel, bound, key, total in rows:
        out += ("\nCREATE TABLE " + quote_ident(nsp) + "." + quote_ident(rel)
                + " PARTITION OF " + quote_ident(pnsp) + "." + quote_ident(prel)
                + " " + (bound or "DEFAULT"))
        if key:
            out += " PARTITION BY " + key
        out += ";"
    more = (total or 0) - len(rows)
    if more > 0:
        out += f"\n-- {more} more partitions are not shown."
    return out


def _comments(cur, oid, full: str, col_notes: list) -> str:
    cur.execute("SELECT pg_catalog.obj_description(%s, 'pg_class')", (oid,))
    row = cur.fetchone()
    out = ""
    if row and row[0] is not None:
        out += f"\nCOMMENT ON TABLE {full} IS {quote_literal(row[0])};"
    for col, note in col_notes:
        out += f"\nCOMMENT ON COLUMN {full}.{quote_ident(col)} IS {quote_literal(note)};"
    return out


def _grants(cur, oid, full: str) -> str:
    """One GRANT line per grantee (and per WITH GRANT OPTION state), the
    privileges in a fixed order."""
    cur.execute(_GRANTS_SQL, (oid,))
    groups: dict[tuple[str, bool], set[str]] = {}
    for is_public, rolname, priv, grantable in cur.fetchall():
        if is_public:
            who = "PUBLIC"
        elif rolname:
            who = quote_ident(rolname)
        else:
            continue  # a dropped role: nothing to name
        groups.setdefault((who, bool(grantable)), set()).add(priv)
    out = ""
    for (who, grantable) in sorted(groups, key=lambda k: (k[0] != "PUBLIC", k[0], k[1])):
        privs = groups[(who, grantable)]
        known = [p for p in _PRIV_ORDER if p in privs] + sorted(privs - set(_PRIV_ORDER))
        out += f"\nGRANT {', '.join(known)} ON TABLE {full} TO {who}"
        out += " WITH GRANT OPTION;" if grantable else ";"
    return out
