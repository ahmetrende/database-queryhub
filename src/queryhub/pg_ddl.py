"""The CREATE script of one PostgreSQL object (2026-11-12, Devart B5).

A view, a materialized view, a function and a procedure come from the
server's own functions (pg_get_viewdef, pg_get_functiondef). A table has no
stored text, so `object_ddl` builds it from the catalog: the columns with
their types, defaults, identity and NOT NULL, the constraints, the partition
key and the indexes that no constraint owns. It leaves out storage options,
grants, comments, triggers and policies.

Every read takes a cursor of a READ ONLY transaction, which the caller opens.
"""
from __future__ import annotations

_REL_SQL = (
    "SELECT c.oid, c.relkind, n.nspname, c.relname "
    "  FROM pg_catalog.pg_class c "
    "  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    " WHERE c.oid = pg_catalog.to_regclass(%s)")
_COLS_SQL = (
    "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull, "
    "       pg_catalog.pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated "
    "  FROM pg_catalog.pg_attribute a "
    "  LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
    " WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped "
    " ORDER BY a.attnum")
_CONS_SQL = (
    "SELECT conname, pg_catalog.pg_get_constraintdef(oid, true) "
    "  FROM pg_catalog.pg_constraint "
    " WHERE conrelid = %s AND contype IN ('p', 'u', 'c', 'f', 'x') "
    " ORDER BY contype <> 'p', conname")
_INDEX_SQL = (
    "SELECT pg_catalog.pg_get_indexdef(i.indexrelid) "
    "  FROM pg_catalog.pg_index i "
    " WHERE i.indrelid = %s "
    "   AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conindid = i.indexrelid) "
    " ORDER BY 1")
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
        oid, relkind, nsp, rel = row
        kind = _RELKINDS.get(relkind)
        if kind is None:
            return None
        full = quote_ident(nsp) + "." + quote_ident(rel)
        if kind in ("view", "materialized view"):
            cur.execute("SELECT pg_catalog.pg_get_viewdef(%s, true)", (oid,))
            body = (cur.fetchone()[0] or "").strip()
            head = "CREATE MATERIALIZED VIEW " if kind == "materialized view" else "CREATE OR REPLACE VIEW "
            return (kind, head + full + " AS\n" + body, False)
        return ("table", _table_script(cur, oid, relkind, full), True)
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


def _table_script(cur, oid, relkind: str, full: str) -> str:
    cur.execute(_COLS_SQL, (oid,))
    lines = []
    for col, typ, notnull, default, identity, generated in cur.fetchall():
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
    cur.execute(_CONS_SQL, (oid,))
    for cname, cdef in cur.fetchall():
        lines.append(f"    CONSTRAINT {quote_ident(cname)} {cdef}")
    head = "CREATE FOREIGN TABLE " if relkind == "f" else "CREATE TABLE "
    script = head + full + " (\n" + ",\n".join(lines) + "\n)"
    if relkind == "p":
        cur.execute("SELECT pg_catalog.pg_get_partkeydef(%s)", (oid,))
        key = cur.fetchone()[0]
        if key:
            script += " PARTITION BY " + key
    script += ";"
    cur.execute(_INDEX_SQL, (oid,))
    for (idx,) in cur.fetchall():
        script += "\n" + idx + ";"
    return script
