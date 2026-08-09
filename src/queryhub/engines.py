"""Per-engine strategy: how the bot treats each database engine.

A target's `engine` column (migrations/068) selects a spec here. Code
that parses SQL, reasons about safety, or opens a connection consults the
spec instead of assuming Postgres everywhere.

Three engines carry a spec today:

  - **postgres** — the full three-tier model (RO/RW/DDL) the bot was
    built around. All the classification fields below are None, so
    query_safety falls back to its own module constants: Postgres
    behavior is byte-identical to before this module existed.

  - **mssql** (SQL Server) — also three-tier. T-SQL parses under the
    sqlglot `tsql` dialect; the leading-word allow-list, RW/DDL keyword
    sets and banned-word reasons are T-SQL-specific (DENY exists, EXEC /
    BULK INSERT / BACKUP / DBCC / USE are banned), and a set of rowset
    functions that turn a plain SELECT into a cross-server / file read
    (OPENROWSET / OPENQUERY / OPENDATASOURCE) is blocked outright, as are
    multi-part table names that leave the connected database — 3-part
    `database.schema.object` (another DB on the instance) and 4-part
    `server.database.schema.object` (a linked server). The PG `SET LOCAL`
    tuning prelude and pre-flight EXPLAIN don't apply.

  - **clickhouse** — read-only. Only SELECT/WITH are accepted (no RW/DDL
    tier), and a set of table/scalar functions that turn a SELECT into
    SSRF / file read / RCE is blocked. Kept here as a spec (safety data)
    for a future wiring; it has no execution path yet.

`WIRED_ENGINES` gates execution: an engine can carry a spec (so its
safety profile is enforced the moment a target is tagged with it) before
its driver + execution dispatch exist. A known-but-unwired engine FAILS
CLOSED — it is never routed through the Postgres path, which would skip
its own dialect and dangerous-function blocklist.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EngineSpec:
    name: str
    # Dialect passed to sqlglot.parse(read=...) / .sql(dialect=...).
    sqlglot_dialect: str
    # Read-only engine: only SELECT / WITH are accepted; there is no
    # RW/DDL credential tier and no data-modifying statement is allowed.
    read_only: bool = False
    # Function names that must never appear in a query, even inside a
    # SELECT. Matched case-insensitively against function / table-function
    # call names in the parsed AST (enforced in the safety layer).
    blocked_functions: frozenset = field(default_factory=frozenset)
    # DEFAULT-DENY allow-list of table functions permitted in a FROM
    # clause (read_only engines only). Anything not listed is rejected.
    table_function_allowlist: frozenset = field(default_factory=frozenset)
    # Schemas a read_only engine must never read from.
    blocked_schemas: frozenset = field(default_factory=frozenset)
    # The namespace an unqualified object name lands in when nothing else
    # says otherwise ('public' on Postgres, 'dbo' on SQL Server). This is a
    # DISPLAY/last-resort default only: the schema catalog records the real
    # schema of every relation, and the UI qualifies from that. It exists so
    # nothing has to hardcode 'public' — which was wrong for most of this
    # fleet, where the majority of tables live outside it.
    default_schema: str = "public"
    # Namespaces that hold the engine's own catalog rather than user data.
    # The snapshot skips them; autocomplete offers them separately.
    system_schemas: frozenset = frozenset({"pg_catalog", "information_schema"})
    # Block table references that name a catalog/database (3-part:
    # database.schema.object) or a server (4-part: server.database.
    # schema.object). For an engine whose login is scoped to one database
    # on one instance (SQL Server), such a reference escapes the approved
    # target — reaching another database, or a linked server. Postgres
    # can't address another database in one connection at all (cross-DB
    # goes through dblink / FDW, which are blocked as functions), and
    # ClickHouse treats `database.table` as normal same-instance
    # addressing — so this stays False for them.
    block_catalog_refs: bool = False

    # --- statement classification -------------------------------------
    # These override query_safety's module-level constants per engine.
    # None = "use the Postgres defaults in query_safety" — so the postgres
    # spec leaves every field None and its behavior is byte-identical to
    # the pre-engine code. A non-Postgres three-tier engine (MSSQL) fills
    # these with its own dialect's keyword sets.
    allowed_leading: frozenset | None = None      # allowed leading words
    banned_leading: dict | None = None            # leading word -> reason
    rw_keywords: frozenset | None = None          # classify as 'rw'
    ddl_keywords: frozenset | None = None          # classify as 'ddl'
    destructive_keywords: frozenset | None = None  # destructive-flag set
    # PG `SET LOCAL <param>` tuning prelude. False => a leading SET is
    # rejected (T-SQL SET is a different construct, not needed for /sql).
    set_local_supported: bool = True
    # Pre-flight EXPLAIN (FORMAT JSON) is Postgres syntax. False => the
    # pre-flight EXPLAIN no-ops (the safety layer is the real guard).
    supports_explain: bool = True

    # Catalog query listing the database's callable routines, run with the RO
    # credential by the schema snapshot. Columns it must return:
    #   schema_name, routine_name, routine_kind, arg_signature, returns
    #
    # None means "this engine has no routine scan", which is the honest state for
    # a spec-only engine: the catalog simply holds no functions for it and
    # autocomplete offers none, rather than a Postgres query being run against a
    # dialect that would reject it. That is the engine dimension the suggestion
    # pool was missing — it was Postgres-shaped for every target.
    routines_sql: str | None = None

    # --- execution ----------------------------------------------------
    # Driver family the executor dispatches on. 'psycopg' is the original
    # (Postgres) path; 'pyodbc' is SQL Server via the Microsoft ODBC
    # driver. Read-only spec-only engines can leave the default.
    driver: str = "psycopg"
    default_port: int = 5432


# Routines a Postgres RO login may call. pg_proc rather than
# information_schema.routines: the latter hides anything the role cannot execute,
# which silently shortens the list for exactly the RO credential doing the scan,
# and it does not distinguish an aggregate from a plain function. prokind covers
# f(unction) / a(ggregate) / w(indow); p(rocedure) is included so CALL targets
# appear. System schemas are excluded here — the built-ins are already offered by
# the editor's static keyword pool, and pg_catalog alone would add ~3000 rows.
_PG_ROUTINES_SQL = """
SELECT n.nspname                                   AS schema_name,
       p.proname                                   AS routine_name,
       CASE p.prokind WHEN 'a' THEN 'aggregate'
                      WHEN 'w' THEN 'window'
                      WHEN 'p' THEN 'procedure'
                      ELSE 'function' END          AS routine_kind,
       pg_get_function_arguments(p.oid)            AS arg_signature,
       pg_get_function_result(p.oid)               AS returns
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
  AND n.nspname NOT LIKE 'pg_temp%'
ORDER BY 1, 2
"""


POSTGRES = EngineSpec(
    name="postgres",
    sqlglot_dialect="postgres",
    read_only=False,
    blocked_functions=frozenset(),  # PG dangerous-fn list stays in ast_safety
    routines_sql=_PG_ROUTINES_SQL,
    driver="psycopg",
    default_port=5432,
)


# ---------------------------------------------------------------------------
# SQL Server (MSSQL) — three-tier, T-SQL dialect.
# ---------------------------------------------------------------------------

# Leading words accepted for a T-SQL statement. No EXPLAIN/SHOW/VALUES-only
# read forms beyond these; no PG maintenance verbs (VACUUM/REINDEX/...).
_MSSQL_ALLOWED_LEADING = frozenset({
    # Read
    "SELECT", "WITH", "VALUES",
    # DML
    "INSERT", "UPDATE", "DELETE", "MERGE",
    # DDL — schema
    "CREATE", "ALTER", "DROP", "TRUNCATE",
    # DDL — permissions (T-SQL adds DENY)
    "GRANT", "REVOKE", "DENY",
})

# Leading words explicitly rejected with a specific reason. EXEC/EXECUTE
# is the big one: it runs stored procedures and dynamic SQL (xp_cmdshell,
# sp_configure, sp_executesql) that the tier model can't classify — so it
# is banned outright, which also neutralizes the xp_/sp_ danger surface
# (those are only reachable via EXEC).
_MSSQL_BANNED_LEADING = {
    "EXEC":        "EXEC / stored-procedure execution is not allowed (it can run dynamic SQL or xp_cmdshell). Submit the actual statement instead.",
    "EXECUTE":     "EXECUTE / stored-procedure execution is not allowed.",
    "USE":         "USE (switching database) is not allowed — choose the target database when you submit.",
    "GO":          "GO batch separators are not allowed — submit a single batch.",
    "BULK":        "BULK INSERT is not allowed (it reads a server-side file).",
    "BACKUP":      "BACKUP is not allowed.",
    "RESTORE":     "RESTORE is not allowed.",
    "DBCC":        "DBCC commands are not allowed.",
    "KILL":        "KILL is not allowed.",
    "SHUTDOWN":    "SHUTDOWN is not allowed.",
    "RECONFIGURE": "RECONFIGURE is not allowed.",
    "WAITFOR":     "WAITFOR (delay) is not allowed.",
    "DECLARE":     "Variable / cursor blocks (DECLARE) are not allowed.",
    "OPEN":        "Cursors (OPEN) are not allowed.",
    "FETCH":       "Cursors (FETCH) are not allowed.",
    "CLOSE":       "Cursors (CLOSE) are not allowed.",
    "DEALLOCATE":  "Cursors (DEALLOCATE) are not allowed.",
    "BEGIN":       "Don't include BEGIN — the bot manages the transaction.",
    "COMMIT":      "Don't include COMMIT — the bot manages the transaction.",
    "ROLLBACK":    "Don't include ROLLBACK — the bot manages the transaction.",
    "SAVE":        "SAVE TRANSACTION is not allowed.",
    "PRINT":       "PRINT is not allowed.",
    "THROW":       "THROW is not allowed.",
}

_MSSQL_RW_KEYWORDS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})
_MSSQL_DDL_KEYWORDS = frozenset({
    "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE", "DENY",
})
_MSSQL_DESTRUCTIVE = frozenset({
    "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
    "GRANT", "REVOKE", "DENY", "INSERT", "CREATE", "MERGE",
})

# Rowset / file / cross-server functions callable INSIDE a SELECT (so they
# would classify as read-only) that read remote hosts, other servers, or
# the server filesystem. Blocked regardless of tier. Names lower-cased.
_MSSQL_BLOCKED = frozenset({
    # rowset / cross-server / ad-hoc distributed queries
    "openrowset", "openquery", "opendatasource", "openxml",
    # file-reading table-valued functions
    "fn_get_audit_file", "fn_trace_gettable", "fn_xe_file_target_read_file",
    # extended / OLE-automation / mail procs (only reachable via EXEC, which
    # is already banned — listed here as defense in depth in case a future
    # parser surfaces them as function calls)
    "xp_cmdshell", "xp_dirtree", "xp_fileexist", "xp_subdirs",
    "xp_regread", "xp_regwrite", "xp_regdeletevalue",
    "sp_configure", "sp_executesql", "sp_oacreate", "sp_oamethod",
    "sp_send_dbmail", "xp_delete_file",
})

# T-SQL routines. sys.objects, not INFORMATION_SCHEMA.ROUTINES: the latter omits
# table-valued functions and reports nothing useful for aggregates. `type` covers
# FN (scalar), IF/TF (inline / multi-statement table-valued), AF (CLR aggregate)
# and P (procedure). Microsoft-shipped objects are excluded — is_ms_shipped filters
# the thousands of sys.* built-ins, which the editor's static pool already covers.
_MSSQL_ROUTINES_SQL = """
SELECT SCHEMA_NAME(o.schema_id)                    AS schema_name,
       o.name                                      AS routine_name,
       CASE o.type WHEN 'P'  THEN 'procedure'
                   WHEN 'AF' THEN 'aggregate'
                   WHEN 'IF' THEN 'function'
                   WHEN 'TF' THEN 'function'
                   ELSE 'function' END             AS routine_kind,
       STUFF((SELECT ', ' + p.name + ' ' + TYPE_NAME(p.user_type_id)
              FROM sys.parameters p
              WHERE p.object_id = o.object_id AND p.parameter_id > 0
              ORDER BY p.parameter_id
              FOR XML PATH('')), 1, 2, '')         AS arg_signature,
       TYPE_NAME((SELECT TOP 1 r.user_type_id FROM sys.parameters r
                  WHERE r.object_id = o.object_id AND r.parameter_id = 0))
                                                   AS returns
FROM sys.objects o
WHERE o.type IN ('FN', 'IF', 'TF', 'AF', 'P')
  AND o.is_ms_shipped = 0
ORDER BY 1, 2
"""


MSSQL = EngineSpec(
    name="mssql",
    sqlglot_dialect="tsql",
    read_only=False,
    default_schema="dbo",
    system_schemas=frozenset({"sys", "INFORMATION_SCHEMA"}),
    blocked_functions=_MSSQL_BLOCKED,
    allowed_leading=_MSSQL_ALLOWED_LEADING,
    banned_leading=_MSSQL_BANNED_LEADING,
    rw_keywords=_MSSQL_RW_KEYWORDS,
    ddl_keywords=_MSSQL_DDL_KEYWORDS,
    destructive_keywords=_MSSQL_DESTRUCTIVE,
    set_local_supported=False,   # T-SQL SET is a different construct
    supports_explain=False,      # no PG-style EXPLAIN (FORMAT JSON)
    block_catalog_refs=True,     # no cross-database / linked-server names
    routines_sql=_MSSQL_ROUTINES_SQL,
    driver="pyodbc",
    default_port=1433,
)


# ---------------------------------------------------------------------------
# ClickHouse — read-only (spec only; no execution path yet).
# ---------------------------------------------------------------------------

_CLICKHOUSE_BLOCKED = frozenset({
    "url", "urlcluster", "remote", "remotesecure", "cluster",
    "clusterallreplicas", "s3", "s3cluster", "gcs", "hdfs", "hdfscluster",
    "azureblobstorage", "azureblobstoragecluster",
    "file", "filecluster",
    "mysql", "postgresql", "jdbc", "odbc", "mongodb", "redis", "sqlite",
    "deltalake", "iceberg", "hudi",
    "executable",
    "joinget", "dictget", "dictgetordefault", "dictgetornull", "dicthas",
    "dictgethierarchy", "dictgetchildren", "dictgetdescendants", "dictisin",
    "dictgetall", "dictgetkeys",
    "addresstoline", "addresstolinewithinlines", "addresstosymbol", "demangle",
    "encrypt", "decrypt", "trydecrypt", "aes_encrypt_mysql", "aes_decrypt_mysql",
})
_CLICKHOUSE_TABLE_FN_ALLOW = frozenset({
    "numbers", "numbers_mt", "generaterandom", "zeros", "values", "null",
})

CLICKHOUSE = EngineSpec(
    name="clickhouse",
    sqlglot_dialect="clickhouse",
    read_only=True,
    blocked_functions=_CLICKHOUSE_BLOCKED,
    table_function_allowlist=_CLICKHOUSE_TABLE_FN_ALLOW,
    blocked_schemas=frozenset({"system", "information_schema"}),
    supports_explain=False,
    default_port=8443,
)


_ENGINES = {e.name: e for e in (POSTGRES, MSSQL, CLICKHOUSE)}


def spec(engine: str | None) -> EngineSpec:
    """Resolve an engine name to its spec.

    None / empty / unknown falls back to Postgres — every legacy target
    is Postgres and the `target_servers.engine` CHECK constraint keeps
    real values to the known set, so this is a safe default rather than a
    silent mis-route.
    """
    return _ENGINES.get((engine or "postgres").strip().lower(), POSTGRES)


# Engines with a WIRED execution path (driver + dispatch + engine-aware
# safety). An engine can carry a spec (safety data) before it can execute.
# A target whose engine is known-but-not-yet-executable must FAIL CLOSED,
# never fall back to the Postgres path. This set grows as each engine's
# execution path is completed and validated against a real host.
# mssql: wired 2026-07-16 after the pyodbc dispatch + AG read-only routing
# (ApplicationIntent=ReadOnly → readable secondary) were validated live
# against a real SQL Server Availability Group (connect / route / execute /
# stream / PII mask).
WIRED_ENGINES = frozenset({"postgres", "mssql"})


def is_executable(engine: str | None) -> bool:
    """True if the bot can actually run queries against this engine yet.
    Distinct from spec(): an engine may have safety data but no execution
    path. None/empty (legacy rows) read as postgres = executable."""
    return (engine or "postgres").strip().lower() in WIRED_ENGINES
