"""Schema catalog: snapshot target schemas into the bot DB + query them.

The snapshot side connects to each target with the RO credential, reads
pg_catalog (tables, columns, indexes, FKs — partitions collapsed into
their parent), and swaps the rows for that (target, database) in one
bot-DB transaction. Run hourly by scripts/refresh_schema_catalog.py.

The query side serves the /sql modal's schema browser and the
tables / schema / findcol subcommands — bot-DB reads only, so browsing
never touches a target and fits Slack's ack deadlines.
"""
from __future__ import annotations

import json
import logging
import re

import psycopg

from . import config as cfg
from . import db
from . import engines

log = logging.getLogger(__name__)

# Everything under these schemas is Postgres plumbing, not user data.
_SYSTEM_SCHEMAS_SQL = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%' "
    "AND n.nspname NOT LIKE 'pg\\_temp%'"
)

_RELKIND_LABELS = {
    "r": "table",
    "p": "partitioned",
    "m": "matview",
    "v": "view",
    "f": "foreign",
}

# One row per visible relation; partition children excluded, parents
# aggregated over pg_partition_tree (rows from leaves, bytes from the
# whole tree). reltuples is -1 before the first VACUUM/ANALYZE.
_TABLES_SQL = f"""
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    c.relkind::text AS relkind,
    CASE WHEN c.relkind = 'p' THEN COALESCE((
            SELECT sum(GREATEST(ch.reltuples, 0))::bigint
            FROM pg_partition_tree(c.oid) t
            JOIN pg_class ch ON ch.oid = t.relid
            WHERE t.isleaf), 0)
         ELSE GREATEST(c.reltuples, 0)::bigint
    END AS row_estimate,
    CASE WHEN c.relkind = 'p' THEN COALESCE((
            SELECT sum(pg_total_relation_size(t.relid))
            FROM pg_partition_tree(c.oid) t), 0)
         WHEN c.relkind IN ('r', 'm') THEN pg_total_relation_size(c.oid)
         ELSE NULL
    END AS total_bytes,
    CASE WHEN c.relkind = 'p' THEN (
            SELECT count(*)::int FROM pg_inherits i WHERE i.inhparent = c.oid)
         ELSE NULL
    END AS partition_count,
    CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END AS partition_key,
    (SELECT jsonb_agg(jsonb_build_object('name', ci.relname,
                                         'def', pg_get_indexdef(ix.indexrelid))
                      ORDER BY ci.relname)
       FROM pg_index ix JOIN pg_class ci ON ci.oid = ix.indexrelid
      WHERE ix.indrelid = c.oid) AS indexes,
    (SELECT jsonb_agg(jsonb_build_object('name', con.conname,
                                         'def', pg_get_constraintdef(con.oid))
                      ORDER BY con.conname)
       FROM pg_constraint con
      WHERE con.conrelid = c.oid AND con.contype = 'f') AS foreign_keys
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm', 'v', 'f')
  AND NOT c.relispartition
  AND {_SYSTEM_SCHEMAS_SQL}
ORDER BY n.nspname, c.relname
"""

_COLUMNS_SQL = f"""
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    a.attnum AS ordinal,
    a.attname AS column_name,
    format_type(a.atttypid, a.atttypmod) AS data_type,
    a.attnotnull AS not_null,
    pg_get_expr(d.adbin, d.adrelid) AS default_expr,
    EXISTS (SELECT 1 FROM pg_index i
             WHERE i.indrelid = c.oid AND i.indisprimary
               AND a.attnum = ANY (i.indkey)) AS is_pk,
    EXISTS (SELECT 1 FROM pg_index i
             WHERE i.indrelid = c.oid
               AND a.attnum = ANY (i.indkey)) AS in_index
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'm', 'v', 'f')
  AND NOT c.relispartition
  AND {_SYSTEM_SCHEMAS_SQL}
ORDER BY n.nspname, c.relname, a.attnum
"""

# Databases hidden from QueryHub everywhere — never snapshotted, never
# listed in any picker/browser on any target. `rdsadmin` refuses non-super
# connections; `postgres` is the empty default maintenance DB nobody
# queries. See also inventory.list_databases_for_endpoint (the /sql modal
# path), which excludes `postgres` too.
_HIDDEN_DATABASES = ("rdsadmin", "postgres")

# Databases worth snapshotting on an instance.
_DATABASES_SQL = """
SELECT datname FROM pg_database
WHERE datallowconn AND NOT datistemplate
  AND datname <> ALL(%s)
ORDER BY datname
"""


# ---------- snapshot (write side) ------------------------------------------


def list_target_databases(target, password: str) -> list[str]:
    """Databases on the target instance, read via its default database."""
    if (getattr(target, "engine", None) or "postgres") == "mssql":
        from . import mssql_exec
        return mssql_exec.catalog_databases(
            target.host, target.port, target.default_database,
            target.username, password)
    with _connect(target, password, target.default_database) as conn:
        with conn.cursor() as cur:
            cur.execute(_DATABASES_SQL, (list(_HIDDEN_DATABASES),))
            return [r[0] for r in cur.fetchall()]


def _connect(target, password: str, database: str):
    return psycopg.connect(
        host=target.host,
        port=target.port,
        dbname=database,
        user=target.username,
        password=password,
        connect_timeout=8,
        **cfg.target_ssl_kwargs(),
        application_name="queryhub-schema-snapshot",
        options="-c statement_timeout=60000 -c default_transaction_read_only=on",
    )


def _routines(target, password: str, database: str) -> list[dict]:
    """Callable routines in one database, per the engine's own catalog query.

    Engine-modular by construction: the SQL lives on the EngineSpec, so an engine
    with no routine scan yields nothing instead of having a Postgres query run
    against it. Failure is NON-FATAL — a missing suggestion pool must never cost
    the tables-and-columns snapshot, which is what the schema browser needs.
    """
    spec = engines.spec(getattr(target, "engine", None))
    if not spec.routines_sql:
        return []
    try:
        if spec.driver == "pyodbc":
            from . import mssql_exec
            return mssql_exec.catalog_rows(
                target.host, target.port, database, target.username, password,
                spec.routines_sql)
        with _connect(target, password, database) as conn:
            with conn.cursor() as cur:
                cur.execute(spec.routines_sql)
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:                       # noqa: BLE001
        log.warning("routine scan failed for %s/%s: %s",
                    getattr(target, "alias", target.id), database, exc)
        return []


def snapshot_database(target, password: str, database: str) -> tuple[int, int]:
    """Snapshot one target database into the bot DB. Returns
    (n_tables, n_columns). The swap (delete old rows + insert new) runs in
    a single bot-DB transaction so readers never see a partial catalog."""
    if (getattr(target, "engine", None) or "postgres") == "mssql":
        from . import mssql_exec
        tables, columns = mssql_exec.catalog_snapshot(
            target.host, target.port, database, target.username, password)
    else:
        with _connect(target, password, database) as conn:
            with conn.cursor() as cur:
                cur.execute(_TABLES_SQL)
                t_cols = [d.name for d in cur.description]
                tables = [dict(zip(t_cols, row)) for row in cur.fetchall()]
                cur.execute(_COLUMNS_SQL)
                c_cols = [d.name for d in cur.description]
                columns = [dict(zip(c_cols, row)) for row in cur.fetchall()]
    routines = _routines(target, password, database)

    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM schema_tables "
            "WHERE target_server_id = %s AND database_name = %s",
            (target.id, database),
        )
        id_by_key: dict[tuple[str, str], int] = {}
        for t in tables:
            cur.execute(
                "INSERT INTO schema_tables (target_server_id, database_name, "
                " schema_name, table_name, relkind, row_estimate, total_bytes, "
                " partition_count, partition_key, indexes, foreign_keys) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    target.id, database,
                    t["schema_name"], t["table_name"],
                    _RELKIND_LABELS.get(t["relkind"], t["relkind"]),
                    t["row_estimate"], t["total_bytes"],
                    t["partition_count"], t["partition_key"],
                    json.dumps(t["indexes"]) if t["indexes"] is not None else None,
                    json.dumps(t["foreign_keys"]) if t["foreign_keys"] is not None else None,
                ),
            )
            id_by_key[(t["schema_name"], t["table_name"])] = cur.fetchone()["id"]
        col_rows = [
            (
                id_by_key[(c["schema_name"], c["table_name"])],
                c["ordinal"], c["column_name"], c["data_type"],
                c["not_null"], c["default_expr"], c["is_pk"], c["in_index"],
            )
            for c in columns
            if (c["schema_name"], c["table_name"]) in id_by_key
        ]
        cur.executemany(
            "INSERT INTO schema_columns (table_id, ordinal, column_name, "
            " data_type, not_null, default_expr, is_pk, in_index) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            col_rows,
        )
        # Same transaction as the tables swap: the catalog is read as one thing,
        # so it is replaced as one thing.
        cur.execute(
            "DELETE FROM schema_functions "
            "WHERE target_server_id = %s AND database_name = %s",
            (target.id, database),
        )
        if routines:
            cur.executemany(
                "INSERT INTO schema_functions (target_server_id, database_name, "
                " schema_name, routine_name, routine_kind, arg_signature, returns) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                # Overloads collapse to one suggestion; see migration 091.
                "ON CONFLICT (target_server_id, database_name, schema_name, "
                "             routine_name) DO NOTHING",
                [(target.id, database, r["schema_name"], r["routine_name"],
                  r["routine_kind"], r.get("arg_signature"), r.get("returns"))
                 for r in routines],
            )
    return len(tables), len(col_rows)


def catalog_functions(target_id: int, database: str,
                      limit: int = 400) -> list[dict]:
    """Routines for one (target, database), schema-qualified.

    Ordered by schema then name so the cap, when it bites, is at least stable
    rather than whatever the planner returned that hour.
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name AS s, routine_name AS n, routine_kind AS k, "
            "       arg_signature AS args, returns AS ret "
            "FROM schema_functions "
            "WHERE target_server_id = %s AND database_name = %s "
            "ORDER BY schema_name, routine_name LIMIT %s",
            (target_id, database, limit),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------- browse / search (read side) -------------------------------------


def search_tables(target_id: int, database: str, pattern: str,
                  limit: int = 50) -> list[dict]:
    """Typeahead + `tables` listing. Empty pattern = biggest tables first."""
    like = f"%{pattern}%" if pattern else "%"
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name, table_name, relkind, row_estimate, "
            "       total_bytes, partition_count "
            "FROM schema_tables "
            "WHERE target_server_id = %s AND database_name = %s "
            "  AND (table_name ILIKE %s OR schema_name || '.' || table_name ILIKE %s) "
            "ORDER BY total_bytes DESC NULLS LAST, schema_name, table_name "
            "LIMIT %s",
            (target_id, database, like, like, limit),
        )
        return cur.fetchall()


def get_table(target_id: int, database: str, table_ref: str):
    """Detail for one table. `table_ref` is `table` or `schema.table`.
    Returns (table_row, columns) on a unique match, a list of candidate
    rows when ambiguous, or None when nothing matches."""
    if "." in table_ref:
        schema, name = table_ref.split(".", 1)
        where, params = (
            "schema_name = %s AND table_name = %s", (schema, name))
    else:
        where, params = ("table_name = %s", (table_ref,))
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM schema_tables "
            f"WHERE target_server_id = %s AND database_name = %s AND {where} "
            f"ORDER BY schema_name",
            (target_id, database, *params),
        )
        matches = cur.fetchall()
        if not matches:
            return None
        if len(matches) > 1:
            return matches
        trow = matches[0]
        cur.execute(
            "SELECT ordinal, column_name, data_type, not_null, default_expr, "
            "       is_pk, in_index "
            "FROM schema_columns WHERE table_id = %s ORDER BY ordinal",
            (trow["id"],),
        )
        return trow, cur.fetchall()


def find_column(pattern: str, target_ids: list[int],
                limit: int = 40) -> list[dict]:
    """Fleet-wide column search across the given (grant-filtered) targets."""
    if not target_ids:
        return []
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t.target_server_id, t.database_name, t.schema_name, "
            "       t.table_name, c.column_name, c.data_type, t.row_estimate "
            "FROM schema_columns c JOIN schema_tables t ON t.id = c.table_id "
            "WHERE c.column_name ILIKE %s AND t.target_server_id = ANY(%s) "
            "  AND t.database_name <> ALL(%s) "
            "ORDER BY t.row_estimate DESC NULLS LAST "
            "LIMIT %s",
            (f"%{pattern}%", target_ids, list(_HIDDEN_DATABASES), limit),
        )
        return cur.fetchall()


def snapshot_info(target_id: int, database: str):
    """Latest snapshot timestamp for a (target, database), or None."""
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT max(snapshot_at) AS ts FROM schema_tables "
            "WHERE target_server_id = %s AND database_name = %s",
            (target_id, database),
        )
        row = cur.fetchone()
        return row["ts"] if row else None


def list_snapshot_databases(target_id: int) -> list[str]:
    """Databases we hold a snapshot for on this target. Hidden databases
    (postgres/rdsadmin) are filtered defensively so a stale row from an
    older snapshot can never surface in a picker."""
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT database_name FROM schema_tables "
            "WHERE target_server_id = %s AND database_name <> ALL(%s) "
            "ORDER BY database_name",
            (target_id, list(_HIDDEN_DATABASES)),
        )
        return [r["database_name"] for r in cur.fetchall()]


# ---------- formatting -------------------------------------------------------


def _fmt_bytes(n) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_rows(n) -> str:
    if n is None:
        return "-"
    n = int(n)
    if n >= 1_000_000_000:
        return f"~{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"~{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"~{n / 1_000:.1f}K"
    return f"~{n}"


def table_summary_line(t: dict) -> str:
    """One-line stats for a table row: rows, size, partitions, kind."""
    bits = []
    if t["relkind"] in ("table", "partitioned"):
        bits.append(f"{_fmt_rows(t['row_estimate'])} rows")
        bits.append(_fmt_bytes(t["total_bytes"]))
    else:
        bits.append(t["relkind"])
    if t.get("partition_count"):
        key = t.get("partition_key") or ""
        key = f" · {key.lower()}" if key else ""
        bits.append(f"{t['partition_count']} partitions{key}")
    return " · ".join(bits)


def format_columns(cols: list[dict]) -> str:
    """Aligned mono text for the columns code block: name, type, markers
    (PK / NN / idx)."""
    if not cols:
        return "(no columns)"
    name_w = min(max(len(c["column_name"]) for c in cols), 32)
    type_w = min(max(len(c["data_type"]) for c in cols), 26)
    lines = []
    for c in cols:
        marks = []
        if c["is_pk"]:
            marks.append("PK")
        if c["not_null"] and not c["is_pk"]:
            marks.append("NN")
        if c["in_index"] and not c["is_pk"]:
            marks.append("idx")
        lines.append(
            f"{c['column_name'][:name_w]:<{name_w}}  "
            f"{c['data_type'][:type_w]:<{type_w}}  "
            f"{' '.join(marks)}".rstrip()
        )
    return "\n".join(lines)


def _index_cols(index_def: str) -> str:
    """`(user_id, status)` out of a CREATE INDEX definition, best-effort."""
    m = re.search(r"USING \w+ \((.*)\)$", index_def or "")
    return f"({m.group(1)})" if m else ""


def format_indexes(indexes) -> str | None:
    """`name(cols) · name(cols)` context line; None when there are none."""
    if not indexes:
        return None
    if isinstance(indexes, str):
        indexes = json.loads(indexes)
    parts = [f"{ix['name']}{_index_cols(ix.get('def', ''))}" for ix in indexes]
    return " · ".join(parts)


def format_fks(fks) -> str | None:
    if not fks:
        return None
    if isinstance(fks, str):
        fks = json.loads(fks)
    return " · ".join(f"{fk['name']}: {fk.get('def', '')}" for fk in fks)
