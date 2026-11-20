"""What uses an object before a DROP or ALTER runs on it.

A DDL request that drops or alters a table, view, materialized view, function
or procedure can break the objects that use it. At submit time QueryHub reads
the catalog of the target and stores those objects on the request. The
approval card in Slack and the web approval queue show them.

The read never blocks or fails a submit. Any error, timeout or missing login
gives None, which the surfaces show as "not checked".

PostgreSQL does not track the code inside a function body. A function that
uses a table in its body is not found, so `bodiesChecked` is false there.
SQL Server records those references, so `bodiesChecked` is true there.
"""
from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp

from . import config as cfg
from . import targets

log = logging.getLogger(__name__)

#: At most this many objects are read for one request.
MAX_OBJECTS = 10
#: At most this many dependents are kept for one object. `more` counts the rest.
MAX_DEPENDENTS = 25
#: At most this many dependents are named in the Slack line for one object.
MAX_NAMED = 5
#: Catalog rows read for one question. A larger number only grows `more`.
_ROW_LIMIT = 1000

ENGINES = ("postgres", "mssql")

_KIND_OF_WORD = {
    "TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "materialized view",
    "FUNCTION": "function",
    "PROCEDURE": "procedure",
    "PROC": "procedure",
}

_IDENT = r'(?:"[^"]+"|\[[^\]]+\]|[A-Za-z_][\w$#@]*)'
_NAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}"
_HEAD_RE = re.compile(
    r"^\s*(DROP|ALTER)\s+(MATERIALIZED\s+VIEW|TABLE|VIEW|FUNCTION|PROCEDURE|PROC)\s+"
    r"(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?",
    re.IGNORECASE)
_NAME_RE = re.compile(_NAME)
_COMMA_NAME_RE = re.compile(rf"\s*,\s*({_NAME})")
_CASCADE_RE = re.compile(r"\bCASCADE\b", re.IGNORECASE)
_LEADING_NOISE = re.compile(r"\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/)", re.DOTALL)


def _dialect(engine: str | None) -> str:
    return "tsql" if (engine or "").lower() == "mssql" else "postgres"


def _strip_leading_comments(text: str) -> str:
    while True:
        match = _LEADING_NOISE.match(text)
        if not match or match.end() == 0:
            return text
        text = text[match.end():]


def _clean_name(name: str, engine: str | None) -> str:
    """The name as written, with no white space around the dots.

    PostgreSQL resolves at most schema.object in one database, so a third
    part (the database) is dropped there."""
    parts = re.findall(_IDENT, name or "")
    if _dialect(engine) == "postgres" and len(parts) > 2:
        parts = parts[-2:]
    return ".".join(p for p in parts if p)


def _regex_targets(text: str, engine: str | None) -> list[dict]:
    """The fallback reader for one statement that sqlglot did not parse."""
    text = _strip_leading_comments(text or "")
    head = _HEAD_RE.match(text)
    if not head:
        return []
    verb = head.group(1).upper()
    word = re.sub(r"\s+", " ", head.group(2).upper())
    kind = _KIND_OF_WORD[word]
    rest = text[head.end():]
    first = _NAME_RE.match(rest)
    if not first:
        return []
    names = [first.group(0)]
    pos = first.end()
    # Only DROP takes a list of names.
    while verb == "DROP":
        more = _COMMA_NAME_RE.match(rest, pos)
        if not more:
            break
        names.append(more.group(1))
        pos = more.end()
    cascade = verb == "DROP" and bool(_CASCADE_RE.search(rest))
    return [{"name": _clean_name(n, engine), "kind": kind, "cascade": cascade}
            for n in names]


def _ast_name(node, dialect: str, engine: str | None) -> str | None:
    """The name of one DROP or ALTER target, as written, or None."""
    if isinstance(node, exp.Schema):
        node = node.this
    if isinstance(node, exp.UserDefinedFunction):
        node = node.this
    if not isinstance(node, exp.Table) or not node.name:
        return None
    # A part that is not a plain identifier (a call such as `f(int)`, for
    # example) goes to the regex reader instead.
    if not node.parts or not all(isinstance(p, exp.Identifier) for p in node.parts):
        return None
    parts = [p.sql(dialect=dialect) for p in node.parts]
    return _clean_name(".".join(parts), engine)


def _ast_targets(stmt, dialect: str, engine: str | None) -> list[dict] | None:
    """The objects one parsed statement names. None asks for the fallback."""
    if isinstance(stmt, exp.Drop):
        word = (stmt.args.get("kind") or "").upper()
        if stmt.args.get("materialized") and word == "VIEW":
            word = "MATERIALIZED VIEW"
        kind = _KIND_OF_WORD.get(word)
        if kind is None:
            return []
        nodes = [stmt.this] + [e for e in (stmt.expressions or [])
                               if isinstance(e, (exp.Table, exp.Schema))]
        names = [_ast_name(n, dialect, engine) for n in nodes if n is not None]
        if not names or any(n is None for n in names):
            return None
        cascade = bool(stmt.args.get("cascade"))
        return [{"name": n, "kind": kind, "cascade": cascade} for n in names]
    if isinstance(stmt, exp.Alter):
        word = (stmt.args.get("kind") or "").upper()
        kind = _KIND_OF_WORD.get(word)
        if kind is None:
            return []
        name = _ast_name(stmt.this, dialect, engine)
        if name is None:
            return None
        return [{"name": name, "kind": kind, "cascade": False}]
    if isinstance(stmt, exp.Command):
        if str(stmt.this or "").upper() in ("DROP", "ALTER"):
            return None
        return []
    return []


def targets_of(sql: str, engine: str | None) -> list[dict]:
    """The objects that the DROP and ALTER statements of `sql` name.

    Each item is {"name", "kind", "cascade"}. Other statements give nothing.
    At most MAX_OBJECTS items, with no repeats."""
    dialect = _dialect(engine)
    found: list[dict] = []
    stmts: list | None
    try:
        stmts = [s for s in sqlglot.parse(sql or "", read=dialect) if s is not None]
    except Exception:
        stmts = None
    if stmts is None:
        # sqlglot failed on the whole text. Read each part on its own.
        for part in (sql or "").split(";"):
            found.extend(_regex_targets(part, engine))
    else:
        for stmt in stmts:
            got = None
            try:
                got = _ast_targets(stmt, dialect, engine)
            except Exception:
                got = None
            if got is None:
                got = _regex_targets(stmt.sql(dialect=dialect), engine)
            found.extend(got)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in found:
        key = (item["name"].lower(), item["kind"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= MAX_OBJECTS:
            break
    return out


# ---- the catalog reads ------------------------------------------------------

_PG_RELATION = (
    "SELECT c.oid, n.nspname, c.relname FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.oid = to_regclass(%s::text)")

_PG_FUNCTIONS = (
    "SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE p.proname = %s::name AND (n.nspname = %s::name "
    "  OR (%s::text IS NULL AND n.nspname = ANY (current_schemas(false))))")

# Views and materialized views whose rewrite rule uses the object. The rule of
# a view depends on the view itself too, so the object is left out.
_PG_VIEWS = (
    "SELECT DISTINCT c.relkind::text, n.nspname, c.relname "
    "FROM pg_depend d "
    "JOIN pg_rewrite r ON r.oid = d.objid "
    "JOIN pg_class c ON c.oid = r.ev_class "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE d.classid = 'pg_rewrite'::regclass "
    "  AND d.refclassid = %s::text::regclass "
    "  AND d.refobjid = ANY (%s::oid[]) AND c.oid <> ALL (%s::oid[]) "
    "ORDER BY 2, 3 LIMIT " + str(_ROW_LIMIT))

_PG_FOREIGN_KEYS = (
    "SELECT con.conname, n.nspname, c.relname FROM pg_constraint con "
    "JOIN pg_class c ON c.oid = con.conrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE con.contype = 'f' AND con.confrelid = %s::oid AND con.conrelid <> %s::oid "
    "ORDER BY 2, 3, 1 LIMIT " + str(_ROW_LIMIT))

_PG_TRIGGERS = (
    "SELECT t.tgname, n.nspname, c.relname FROM pg_trigger t "
    "JOIN pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE t.tgfoid = ANY (%s::oid[]) AND NOT t.tgisinternal "
    "ORDER BY 2, 3, 1 LIMIT " + str(_ROW_LIMIT))

_RELKIND = {"v": "view", "m": "materialized view"}


def _unquote(part: str, engine: str | None) -> str:
    """One name part as the catalog stores it."""
    if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
        return part[1:-1].replace('""', '"')
    if len(part) >= 2 and part[0] == "[" and part[-1] == "]":
        return part[1:-1]
    # PostgreSQL folds a name with no quotes to lower case.
    return part.lower() if _dialect(engine) == "postgres" else part


def _split(name: str) -> list[str]:
    return re.findall(_IDENT, name)


def _object_entry(obj: dict, found: bool, dependents: list[dict]) -> dict:
    return {
        "name": obj["name"],
        "kind": obj["kind"],
        "found": found,
        "cascade": bool(obj.get("cascade")),
        "dependents": dependents[:MAX_DEPENDENTS],
        "more": max(0, len(dependents) - MAX_DEPENDENTS),
    }


def _pg_dependents(cur, obj: dict) -> dict:
    if obj["kind"] in ("function", "procedure"):
        parts = [_unquote(p, "postgres") for p in _split(obj["name"])]
        schema = parts[-2] if len(parts) >= 2 else None
        cur.execute(_PG_FUNCTIONS, (parts[-1], schema, schema))
        oids = [r[0] for r in cur.fetchall()]
        if not oids:
            return _object_entry(obj, False, [])
        deps: list[dict] = []
        cur.execute(_PG_VIEWS, ("pg_proc", oids, []))
        for relkind, nsp, rel in cur.fetchall():
            deps.append({"kind": _RELKIND.get(relkind, "view"), "name": f"{nsp}.{rel}"})
        cur.execute(_PG_TRIGGERS, (oids,))
        for tgname, nsp, rel in cur.fetchall():
            deps.append({"kind": "trigger", "name": f"{nsp}.{rel}", "detail": tgname})
        return _object_entry(obj, True, deps)

    cur.execute(_PG_RELATION, (obj["name"],))
    row = cur.fetchone()
    if not row:
        return _object_entry(obj, False, [])
    oid = row[0]
    deps = []
    cur.execute(_PG_VIEWS, ("pg_class", [oid], [oid]))
    for relkind, nsp, rel in cur.fetchall():
        deps.append({"kind": _RELKIND.get(relkind, "view"), "name": f"{nsp}.{rel}"})
    cur.execute(_PG_FOREIGN_KEYS, (oid, oid))
    for conname, nsp, rel in cur.fetchall():
        deps.append({"kind": "foreign key", "name": f"{nsp}.{rel}", "detail": conname})
    return _object_entry(obj, True, deps)


def _read_postgres(target, database: str, objects: list[dict],
                   default_schema: str | None) -> dict:
    import psycopg

    from . import executor  # local: the executor module is large
    # The same search_path as the DDL run, so a name resolves to the object
    # that the run changes. It refuses a bad schema name with ValueError.
    path_sql = "SET LOCAL search_path = " + executor.search_path_for("ddl", default_schema)
    db_user, password = targets.get_credentials(target.id, "ro")
    with psycopg.connect(
        host=target.host,
        port=target.port,
        dbname=database,
        user=db_user,
        password=password,
        connect_timeout=2,
        **cfg.target_ssl_kwargs(target.host),
        application_name=f"dba-slack-bot:dependents target={target.alias}"[:63],
        options="-c statement_timeout=2000 -c idle_in_transaction_session_timeout=3000",
    ) as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(path_sql)
        out = [_pg_dependents(cur, obj) for obj in objects]
        conn.rollback()
    return {"engine": "postgres", "objects": out, "bodiesChecked": False}


_MSSQL_KIND = {
    "V": "view", "P": "procedure", "PC": "procedure", "TR": "trigger",
    "FN": "function", "IF": "function", "TF": "function", "FS": "function",
    "FT": "function", "U": "table",
}

_MSSQL_OBJECT_ID = "SELECT OBJECT_ID(?)"

_MSSQL_CAN_SEE_DEPENDENCIES = (
    "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')")

_MSSQL_USERS = (
    "SELECT DISTINCT RTRIM(o.type), s.name, o.name "
    "FROM sys.sql_expression_dependencies d "
    "JOIN sys.objects o ON o.object_id = d.referencing_id "
    "JOIN sys.schemas s ON s.schema_id = o.schema_id "
    "WHERE d.referenced_id = ? AND d.referencing_id <> ? "
    "ORDER BY 2, 3")

_MSSQL_FOREIGN_KEYS = (
    "SELECT fk.name, s.name, t.name FROM sys.foreign_keys fk "
    "JOIN sys.tables t ON t.object_id = fk.parent_object_id "
    "JOIN sys.schemas s ON s.schema_id = t.schema_id "
    "WHERE fk.referenced_object_id = ? AND fk.parent_object_id <> ? "
    "ORDER BY 2, 3, 1")


def _mssql_dependents(cur, obj: dict) -> dict:
    cur.execute(_MSSQL_OBJECT_ID, (obj["name"],))
    row = cur.fetchone()
    oid = row[0] if row else None
    if oid is None:
        return _object_entry(obj, False, [])
    deps: list[dict] = []
    cur.execute(_MSSQL_USERS, (oid, oid))
    for otype, sch, name in cur.fetchmany(_ROW_LIMIT):
        kind = _MSSQL_KIND.get((otype or "").strip().upper(), "object")
        deps.append({"kind": kind, "name": f"{sch}.{name}"})
    if obj["kind"] == "table":
        cur.execute(_MSSQL_FOREIGN_KEYS, (oid, oid))
        for fkname, sch, name in cur.fetchmany(_ROW_LIMIT):
            deps.append({"kind": "foreign key", "name": f"{sch}.{name}", "detail": fkname})
    return _object_entry(obj, True, deps)


def _read_mssql(target, database: str, objects: list[dict]) -> dict | None:
    from . import mssql_exec
    db_user, password = targets.get_credentials(target.id, "ro")
    conn = mssql_exec.connect(
        target.host, target.port, database, db_user, password,
        timeout_sec=3, read_only=True, app_name="QueryHub dependents")
    try:
        cur = conn.cursor()
        # sys.sql_expression_dependencies shows rows only to a login with
        # VIEW DEFINITION. The RO login of deploy/grant_mssql.sql does not
        # hold it, and an empty list would then read as "nothing uses it".
        # So without the permission the answer is "not checked".
        cur.execute(_MSSQL_CAN_SEE_DEPENDENCIES)
        row = cur.fetchone()
        if not row or row[0] != 1:
            conn.rollback()
            return None
        out = [_mssql_dependents(cur, obj) for obj in objects]
        conn.rollback()
    finally:
        conn.close()
    return {"engine": "mssql", "objects": out, "bodiesChecked": True}


def read(target, engine: str | None, objects: list[dict],
         default_schema: str | None = None, *,
         database: str | None = None) -> dict | None:
    """Read what uses each object, with one short read-only connection.

    `database` is the database of the request. Without it the default
    database of the target applies. Returns None on any error: the caller
    shows "not checked" and the submit goes on."""
    if not objects:
        return None
    engine = (engine or "postgres").lower()
    if engine not in ENGINES:
        return None
    dbname: str = database or getattr(target, "default_database", None) or ""
    try:
        if engine == "mssql":
            return _read_mssql(target, dbname, objects[:MAX_OBJECTS])
        return _read_postgres(target, dbname, objects[:MAX_OBJECTS], default_schema)
    except Exception as e:  # noqa: BLE001 - this read must never fail a submit
        log.warning("ddl dependents read failed (target=%s db=%s): %s",
                    getattr(target, "alias", None), dbname, e)
        return None


# ---- the Slack line ---------------------------------------------------------

def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _object_line(obj: dict) -> str:
    name = obj.get("name") or "?"
    if not obj.get("found"):
        return f"{name}: object not found"
    deps = obj.get("dependents") or []
    more = int(obj.get("more") or 0)
    if not deps and not more:
        return f"{name}: nothing found that uses it"
    groups: dict[str, list[str]] = {}
    for d in deps:
        groups.setdefault(d.get("kind") or "object", []).append(d.get("name") or "?")
    budget = MAX_NAMED
    parts = []
    for kind, names in groups.items():
        shown = names[:budget]
        budget -= len(shown)
        rest = len(names) - len(shown)
        label = f"{len(names)} {kind}" + ("s" if len(names) != 1 else "")
        if shown:
            inner = ", ".join(shown) + (f" and {rest} more" if rest else "")
            label += f" ({inner})"
        parts.append(label)
    if more:
        parts.append(f"{more} more")
    line = f"{name}: used by {_join(parts)}"
    if obj.get("cascade"):
        line += ". CASCADE drops these too"
    return line


def summary_text(result: dict | None) -> str | None:
    """One plain line per object, for the Slack approval card. None when
    there is nothing to show."""
    if not result or not isinstance(result, dict):
        return None
    objects = result.get("objects") or []
    if not objects:
        return None
    return "\n".join(_object_line(o) for o in objects)
