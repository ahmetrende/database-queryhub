"""The schema tools that the MCP door and the AI assistant share (M13).

Two callers ask the same questions about a database: which tables it has,
and everything about one table. The MCP door (`mcp_server.tools`) asks for a
program that a person connected. The AI assistant of the web editor asks for
the person at the editor. Both go through these functions, so both get the
same grant check, the same caps, the same answer and the same audit row.

What these functions return is schema, never data:

- The table list and the columns come from the hourly catalog (bot DB).
- `table_detail` also reads the target live, with its read login: the CREATE
  script, the triggers and the NUMERIC statistics. No query reads a
  statistic that holds a column value (`most_common_vals`,
  `histogram_bounds`, a SQL Server histogram). `table_facts.py` and
  `mssql_exec.table_facts` say so in their SQL.

Identity is an argument here (`uid`), on purpose. The MCP tools resolve the
caller from the transport and pass it in. They never take it from the client.
The web route of the assistant passes the user of the session.

`named` turns a tool name into the text that the caller's model sees: the
MCP door lists its tools with a prefix, the assistant does not.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

import psycopg

from . import audit, db, db_scope, pg_ddl, pii, table_facts, targets, teams
from . import config as cfg

log = logging.getLogger(__name__)

#: Table names one listing may return. The largest catalogue here holds
#: 2,727 tables. All of them, with columns, is 2.6 MB of JSON.
MAX_TABLES = 300

#: Tables one filtered call may describe in full. A loose filter still
#: matches hundreds, and each carries every column.
MAX_DETAIL_TABLES = 25

#: The longest CREATE script that `table_detail` returns, in characters.
MAX_SCRIPT_CHARS = 30_000

#: The longest table name that `table_detail` accepts.
MAX_NAME_CHARS = 300


class ToolError(Exception):
    """A refusal the caller can act on. `code` is short and log-safe."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def bare(name: str) -> str:
    """The default `named`: the tool name in backticks, with no prefix."""
    return f"`{name}`"


Named = Callable[[str], str]


# --- who may read what --------------------------------------------------------


def granted_target(uid: str, connection: str, *, named: Named = bare):
    """(target, grant) for an alias the caller named. Refused if they hold no
    grant on it.

    Unknown and ungranted are the same answer. Two answers would let a caller
    find real server names by watching which refusal comes back.
    """
    t = targets.by_alias(connection)
    grant = teams.effective_grant_for_user(uid, t.id) if t is not None else None
    if t is None or grant is None:
        raise ToolError("unknown_connection",
                        f"There is no connection named '{connection}' that you "
                        f"can use. Call {named('list_connections')} to see "
                        f"your connections.")
    return t, grant


def require_database(t, grant: dict, database: str) -> None:
    """Refuse a database outside the person's list, with one answer whether it
    exists or not. The catalog alone cannot tell the two apart for an
    unrestricted grant, so the list decides."""
    usable = db_scope.usable_databases(t, grant)
    if database not in usable:
        raise ToolError("no_access",
                        db_scope.no_access_message(t.alias, database, usable))


def granted_database(uid: str, connection: str, database: str, *,
                     named: Named = bare):
    """`granted_target` and `require_database` in one call: (target, grant)."""
    t, grant = granted_target(uid, connection, named=named)
    require_database(t, grant, database)
    return t, grant


# --- the table list -----------------------------------------------------------


def table_names(t, database: str) -> dict:
    """The table NAMES of one database, capped at `MAX_TABLES`.

    The caller checks the grant first. Names alone are ~30x smaller than names
    with columns, and they are what a model needs first: it asks what exists,
    then asks about one thing. A cap that stays quiet reads as "that is all
    there is", so a truncated list says so.
    """
    rows = db.fetch_all(
        "SELECT st.schema_name, st.table_name, st.relkind, "
        "       COUNT(sc.id) AS columns "
        "  FROM schema_tables st "
        "  LEFT JOIN schema_columns sc ON sc.table_id = st.id "
        " WHERE st.target_server_id = %s AND st.database_name = %s "
        " GROUP BY st.schema_name, st.table_name, st.relkind "
        " ORDER BY st.schema_name, st.table_name "
        " LIMIT %s", (t.id, database, MAX_TABLES + 1))
    if not rows:
        raise ToolError(
            "no_catalog",
            f"The schema of '{database}' on '{t.alias}' is not catalogued "
            f"yet. The first snapshot has not run.")
    more = len(rows) > MAX_TABLES
    rows = rows[:MAX_TABLES]
    return {
        "connection": t.alias, "database": database,
        "tables": [{"schema": r["schema_name"], "table": r["table_name"],
                    "kind": r["relkind"], "columns": r["columns"]}
                   for r in rows],
        "truncated": more,
        "note": (f"This database has more than {MAX_TABLES} tables. The list "
                 f"shows the first {MAX_TABLES}. To see columns, pass `table` "
                 f"with part of a table name."
                 if more else
                 "To see columns, pass `table` with part of a table name."),
    }


def tables_matching(t, database: str, table: str, *, named: Named = bare) -> dict:
    """The columns of the tables whose name contains `table`, capped at
    `MAX_DETAIL_TABLES`.

    SQL chooses and caps the tables, and the query reads only their columns.
    A loose filter (`table="a"`) matched 2,073 tables on the largest database.
    A table with no catalogued column is not a match.
    """
    want = (table or "").strip().lower()
    like = f"%{want}%"
    rows = db.fetch_all(
        "WITH hit AS ("
        "  SELECT st.id, st.schema_name, st.table_name, st.relkind "
        "    FROM schema_tables st "
        "   WHERE st.target_server_id = %s AND st.database_name = %s "
        "     AND lower(st.table_name) LIKE %s "
        "     AND EXISTS (SELECT 1 FROM schema_columns x WHERE x.table_id = st.id) "
        "   ORDER BY st.schema_name, st.table_name "
        "   LIMIT %s) "
        "SELECT h.schema_name, h.table_name, h.relkind, "
        "       sc.column_name, sc.data_type, sc.is_pk "
        "  FROM hit h JOIN schema_columns sc ON sc.table_id = h.id "
        " ORDER BY h.schema_name, h.table_name, sc.column_name",
        (t.id, database, like, MAX_DETAIL_TABLES))
    if not rows:
        raise ToolError(
            "no_match",
            f"No catalogued table in '{database}' on '{t.alias}' matches "
            f"'{table}'. To see the table names, call "
            f"{named('describe_database')} without `table`.")
    tables: dict[tuple, dict] = {}
    for r in rows:
        e = tables.setdefault((r["schema_name"], r["table_name"]),
                              {"schema": r["schema_name"],
                               "table": r["table_name"],
                               "kind": r["relkind"], "columns": []})
        e["columns"].append({"name": r["column_name"], "type": r["data_type"],
                             "pk": bool(r["is_pk"])})
    matched = len(tables)
    if matched >= MAX_DETAIL_TABLES:
        # Only a full page can hide more. Counting reads no columns.
        counted = db.fetch_one(
            "SELECT count(*) AS n FROM schema_tables st "
            " WHERE st.target_server_id = %s AND st.database_name = %s "
            "   AND lower(st.table_name) LIKE %s "
            "   AND EXISTS (SELECT 1 FROM schema_columns x WHERE x.table_id = st.id)",
            (t.id, database, like))
        matched = counted["n"] if counted else matched
    return {
        "connection": t.alias, "database": database, "match": table,
        "tables": list(tables.values()),
        "truncated": matched > len(tables),
        "note": (f"{matched} tables matched. This result shows {len(tables)} of "
                 f"them. Use a more specific `table` filter."
                 if matched > len(tables) else None),
    }


# --- one table in full ------------------------------------------------------


# One part of a dotted name: "x", [x] or `x` (a doubled closing quote inside
# stands for one), or a bare word.
_PART_RE = re.compile(
    r'\s*(?:"((?:[^"]|"")*)"|\[((?:[^\]]|\]\])*)\]|`((?:[^`]|``)*)`|([^\s."\[\]`]+))\s*')


def name_parts(name: str) -> list[str] | None:
    """`dbo.[Order Lines]` -> ["dbo", "Order Lines"]. None when the name does
    not parse or a part is empty."""
    parts, pos = [], 0
    while True:
        m = _PART_RE.match(name, pos)
        if not m:
            return None
        dq, br, bt, plain = m.groups()
        part = (dq.replace('""', '"') if dq is not None
                else br.replace("]]", "]") if br is not None
                else bt.replace("``", "`") if bt is not None else plain)
        if not part:
            return None
        parts.append(part)
        pos = m.end()
        if pos == len(name):
            return parts
        if name[pos] != ".":
            return None
        pos += 1


_TABLE_ROW_SQL = (
    "SELECT id, schema_name, table_name, relkind, row_estimate, total_bytes, "
    "       partition_count, partition_key, indexes, foreign_keys, snapshot_at "
    "  FROM schema_tables "
    " WHERE target_server_id = %s AND database_name = %s "
    "   AND lower(table_name) = lower(%s) "
    "   AND (%s::text IS NULL OR lower(schema_name) = lower(%s)) "
    " ORDER BY schema_name, table_name LIMIT 20")


def find_table(t, database: str, table: str, *, named: Named = bare) -> dict:
    """The catalog row of the one table that `table` names.

    `table` is `table`, `schema.table` or `database.schema.table`, quoted or
    not. Names compare without case, and an exact-case match wins. The lookup
    searches only the catalog of THIS database, so a name never reaches a
    table of a database outside the grant.
    """
    raw = (table or "").strip()
    parts = name_parts(raw) if raw and len(raw) <= MAX_NAME_CHARS else None
    if parts and len(parts) == 3:
        if parts[0].casefold() != database.casefold():
            parts = None
        else:
            parts = parts[1:]
    if not parts or len(parts) > 2:
        raise ToolError(
            "no_match",
            f"'{table}' is not a table name. Write `table` or `schema.table`. "
            f"To see the table names, call {named('describe_database')}.")
    schema, name = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
    rows = db.fetch_all(_TABLE_ROW_SQL, (t.id, database, name, schema, schema))
    if not rows and schema is not None and schema.casefold() == database.casefold():
        # ClickHouse and Athena write `database.table`.
        rows = db.fetch_all(_TABLE_ROW_SQL, (t.id, database, name, None, None))
    exact = [r for r in rows if r["table_name"] == name
             and (schema is None or r["schema_name"] == schema)]
    rows = exact or rows
    if not rows:
        raise ToolError(
            "no_match",
            f"No catalogued table in '{database}' on '{t.alias}' is named "
            f"'{table}'. To find the name, call {named('describe_database')} "
            f"with part of it in `table`.")
    if len(rows) > 1:
        names = ", ".join(f"`{r['schema_name']}.{r['table_name']}`" for r in rows[:10])
        raise ToolError(
            "ambiguous",
            f"'{table}' names more than one table: {names}. Write the name "
            f"as `schema.table`.")
    return rows[0]


def _json_list(value) -> list:
    if not value:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return list(value)


def _catalog_part(t, database: str, trow: dict) -> dict:
    """What the hourly catalog knows about one table. This reads no target."""
    cols = db.fetch_all(
        "SELECT column_name, data_type, not_null, default_expr, is_pk, in_index "
        "  FROM schema_columns WHERE table_id = %s ORDER BY ordinal",
        (trow["id"],))
    names = [c["column_name"] for c in cols]
    masked = {names[i] for i in pii.column_pii_map(names)} if names else set()
    columns = []
    for c in cols:
        entry = {"name": c["column_name"], "type": c["data_type"],
                 "nullable": not c["not_null"], "default": c["default_expr"],
                 "pk": bool(c["is_pk"]), "indexed": bool(c["in_index"])}
        if c["column_name"] in masked:
            # QueryHub masks the values of this column in a result.
            entry["masked"] = True
        columns.append(entry)
    snap = trow.get("snapshot_at")
    return {
        "connection": t.alias, "database": database,
        "schema": trow["schema_name"], "table": trow["table_name"],
        "kind": trow["relkind"],
        "rowEstimate": trow.get("row_estimate"),
        "totalBytes": trow.get("total_bytes"),
        "partitions": trow.get("partition_count"),
        "partitionKey": trow.get("partition_key"),
        "columns": columns,
        "indexes": [{"name": i.get("name"), "def": i.get("def")}
                    for i in _json_list(trow.get("indexes"))],
        "foreignKeys": [{"name": f.get("name"), "def": f.get("def")}
                        for f in _json_list(trow.get("foreign_keys"))],
        "catalogAt": snap.isoformat() if snap is not None else None,
    }


def _read_login(t) -> tuple[str, str] | None:
    """(user, password) of the target's read login, or None when it has none."""
    try:
        user, password = targets.get_credentials(t.id, "ro")
    except Exception:                               # noqa: BLE001
        log.warning("schema_tools: no read login for target %s", t.id, exc_info=True)
        return None
    if password == targets.SENTINEL_PASSWORD:
        return None
    return user, password


def _live_postgres(t, database: str, schema: str, table: str, login) -> dict:
    user, password = login
    with psycopg.connect(
        host=t.host, port=t.port, dbname=database,
        user=user, password=password, connect_timeout=3,
        **cfg.target_ssl_kwargs(t.host), application_name="dba-slack-bot:schema-tools",
        options="-c statement_timeout=5000",
    ) as cn, cn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        got = pg_ddl.object_ddl(cur, schema, table)
        facts = table_facts.pg_facts(cur, schema, table)
    out: dict = {"createScript": got[1] if got else None}
    if facts:
        out.update(facts)
    return out


def _live_mssql(t, database: str, schema: str, table: str, login) -> dict:
    from . import mssql_exec
    user, password = login
    got = mssql_exec.object_ddl(t.host, t.port, database, user, password, schema, table)
    facts = mssql_exec.table_facts(t.host, t.port, database, user, password, schema, table)
    out: dict = {"createScript": got[1] if got else None}
    if facts:
        out.update({"triggers": facts["triggers"], "statistics": facts["stats"],
                    "liveRows": facts["rows"]})
    return out


def _live_other(t, engine: str, database: str, table: str, login) -> dict:
    if engine == "athena":
        from . import athena_exec
        got = athena_exec.object_ddl(athena_exec.config_of(t), database, table.lower())
    else:
        from . import clickhouse_exec
        user, password = login
        got = clickhouse_exec.object_ddl(t.host, t.port, database, user, password, table)
    return {"createScript": got[1] if got else None}


def _live_part(t, database: str, trow: dict) -> dict:
    """The CREATE script, triggers and numeric statistics, read live with the
    read login of the target. Raises on a failed read."""
    engine = (getattr(t, "engine", None) or "postgres").strip().lower()
    schema, table = trow["schema_name"], trow["table_name"]
    if engine == "athena":
        return _live_other(t, engine, database, table, None)
    login = _read_login(t)
    if login is None:
        raise ToolError("no_read_login",
                        "This connection has no read login. QueryHub can show "
                        "the catalog part only.")
    if engine == "postgres":
        return _live_postgres(t, database, schema, table, login)
    if engine == "mssql":
        return _live_mssql(t, database, schema, table, login)
    if engine == "clickhouse":
        return _live_other(t, engine, database, table, login)
    raise ToolError("unsupported", "QueryHub cannot read this engine live.")


def table_detail(uid: str, actor_name: str | None, t, database: str, table: str,
                 *, via: str, named: Named = bare) -> dict:
    """Everything about one table, for a caller that `granted_database` let in.

    The catalog part always comes back. The live part (CREATE script,
    triggers, statistics) comes back when the target answers. When it does
    not, `liveError` says so and the catalog part still helps.

    Each call writes one audit row (`table_detail_read`) with the table and
    the door (`via`): the live part opens a connection to the target with
    its read login, as `GET .../ddl` does.
    """
    trow = find_table(t, database, table, named=named)
    out = _catalog_part(t, database, trow)
    live_ok = False
    try:
        live = _live_part(t, database, trow)
        script = live.get("createScript")
        if script and len(script) > MAX_SCRIPT_CHARS:
            live["createScript"] = script[:MAX_SCRIPT_CHARS]
            live["createScriptTruncated"] = True
        out.update(live)
        live_ok = True
    except ToolError as e:
        out["liveError"] = e.message
    except Exception as e:                          # noqa: BLE001
        # A driver's own text can name a host or a login. The caller gets a
        # fixed sentence and the log keeps the type.
        log.info("schema_tools: live read of %s.%s in %s/%s failed: %s",
                 trow["schema_name"], trow["table_name"], t.alias, database,
                 type(e).__name__)
        out["liveError"] = ("The server did not answer. This answer holds the "
                            "catalog part only.")
    audit.log(None, uid, actor_name, "table_detail_read",
              {"target_id": t.id, "database": database,
               "object": f"{trow['schema_name']}.{trow['table_name']}",
               "via": via, "live": live_ok})
    return out
