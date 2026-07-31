"""SQL Server (MSSQL) execution helpers — pyodbc via the Microsoft ODBC
Driver 18.

Deliberately isolated from the Postgres executor path. The bot's Postgres
execution stays on its raw psycopg connection, byte-identical; the executor
dispatches here ONLY for an `engine='mssql'` target — and only once mssql is
added to `engines.WIRED_ENGINES` and this path has been validated against a
real host. Until then a mssql target fails closed in the executor.

`pyodbc` is an OPTIONAL dependency (`pip install '.[mssql]'`) and the host
needs the system `msodbcsql18` driver, so pyodbc is imported LAZILY — a
Postgres-only host never has to have it installed.

Tier model is identical to Postgres: the caller selects the RO / RW / DDL
login (targets.get_credentials) by the query's classified tier; connection,
timeout, and statement execution are the only engine-specific parts.
"""
from __future__ import annotations

import logging
import struct
from datetime import datetime, timedelta, timezone

from . import config as cfg
from . import db

log = logging.getLogger(__name__)

# SQL_SS_TIMESTAMPOFFSET. pyodbc ships no mapping for it, so *any* query touching
# a `datetimeoffset` column died outright:
#
#   pyodbc.ProgrammingError: ('ODBC SQL type -155 is not yet supported.
#                              column-index=6 type=-155', 'HY106')
#
# Not a formatting nicety — `SELECT SYSDATETIMEOFFSET()` and every table holding
# a `datetimeoffset` were unusable through the gateway, and the failure came
# from the driver rather than from anything a user could see and avoid. Found
# while probing sub-second precision, 2026-07-30.
SQL_SS_TIMESTAMPOFFSET = -155

# The ODBC struct behind that type, verified against the server's OWN rendering
# rather than taken from documentation (2026-07-30, against a live SQL Server):
# 20 bytes, little-endian, six shorts, then a 4-byte fraction in NANOSECONDS,
# then the signed offset. Both offset fields carry the sign, so a negative zone
# arrives as (-5, -30) and the timedelta comes out right — checked with
# TODATETIMEOFFSET rather than assumed.
#
#   raw   ea0707001e000f001d00270078b0ab1703000000
#   SQL   2026-07-30 15:29:39.3971278 +03:00
#   ours  2026-07-30 15:29:39.397127+03:00
#
# The seventh digit is lost coming in: datetimeoffset(7) counts 100ns ticks and a
# Python datetime carries microseconds. That is the transport's ceiling, not a
# choice, and `cell_format` deliberately does not pad it back.
_DTO_STRUCT = "<6hI2h"


def parse_datetimeoffset(value):
    """pyodbc output converter for `datetimeoffset` -> timezone-aware datetime.

    Fail-VISIBLE, which is the deliberate middle of three bad options: raising
    would reproduce the bug this fixes (a query that already produced rows dies
    at fetch), and returning None would erase a value while claiming success. The
    struct is fixed by the ODBC spec so this should never miss; if it somehow
    does, the cell shows hex — obviously not a timestamp, and traceable to a row.
    """
    if value is None:
        return None
    try:
        y, mo, d, h, mi, s, frac_ns, tz_h, tz_m = struct.unpack(_DTO_STRUCT, value)
        return datetime(y, mo, d, h, mi, s, frac_ns // 1000,
                        timezone(timedelta(hours=tz_h, minutes=tz_m)))
    except Exception:
        log.warning("unparseable datetimeoffset (%s bytes) — passed through as hex",
                    len(value) if value is not None else "?", exc_info=True)
        try:
            return value.hex()
        except Exception:
            return None

# Microsoft ODBC Driver 18 for SQL Server. Kept as a constant so a host with
# a different installed driver version can override it via bot_config
# (mssql_odbc_driver) without a code change.
_DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"

# Connect timeout (seconds) — the TCP/login handshake bound, separate from the
# per-query statement timeout. Matches the Postgres path's hardcoded 15s.
_CONNECT_TIMEOUT = 15


def _brace(value: object) -> str:
    """Brace-quote an ODBC connection-string value so special characters
    (``;`` ``{`` ``}`` ``=``) in a password / identifier can't break out of
    the field. ODBC escapes a literal ``}`` by doubling it."""
    return "{" + str(value).replace("}", "}}") + "}"


def odbc_connection_string(
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    *,
    driver: str | None = None,
    encrypt: bool = True,
    trust_server_cert: bool = False,
    connect_timeout: int = _CONNECT_TIMEOUT,
    app_name: str = "QueryHub",
    application_intent: str | None = None,
    multi_subnet_failover: bool = True,
) -> str:
    """Build a pyodbc/ODBC connection string for a SQL Server target.

    TLS is on by default (``Encrypt=yes``). ``trust_server_cert`` controls
    whether the server certificate must chain to a trusted CA (default: it
    must — verify). Set it True per-target only for a server with a private/
    self-signed cert, the same deliberate trade-off the Postgres side makes
    with ``sslmode=require`` on RDS. UID/PWD/DATABASE are brace-quoted so a
    special-char password can't inject extra keywords.

    ``application_intent="ReadOnly"`` makes an Availability-Group listener
    route the connection to a READABLE SECONDARY (read-only routing). The
    executor sets it for RO-tier queries so reads land on the replica, and
    leaves it off for RW/DDL so they reach the primary. ``multi_subnet_
    failover`` is recommended for an AG listener (parallel connect to all
    listener IPs = fast failover across subnets); on by default.
    """
    drv = driver or (cfg.get_setting("mssql_odbc_driver", "") or _DEFAULT_DRIVER)
    parts = [
        f"DRIVER={{{drv}}}",
        f"SERVER={host},{int(port)}",
        f"DATABASE={_brace(database)}",
        f"UID={_brace(user)}",
        f"PWD={_brace(password)}",
        f"Encrypt={'yes' if encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if trust_server_cert else 'no'}",
        f"Connection Timeout={int(connect_timeout)}",
        f"APP={app_name}",
    ]
    if application_intent:
        parts.append(f"ApplicationIntent={application_intent}")
    if multi_subnet_failover:
        parts.append("MultiSubnetFailover=yes")
    return ";".join(parts) + ";"


# Two more type codes pyodbc will not dispatch, with the same consequence as
# datetimeoffset: one such column and the WHOLE query dies, however healthy the
# other columns are.
#
#   -16   SQL_VARIANT   a column that holds a different type per row
#   -151  SQL_SS_UDT    hierarchyid, geography, geometry
#
# Measured on the live SQL Server 2026-07-30, and the two behave differently:
#
#   sql_variant('merhaba')  -> 'merhaba' (str)        already decoded
#   sql_variant(1234.56)    -> Decimal('1234.56')     already decoded
#   hierarchyid '/1/2/3/'   -> b'\x5b\x5e'            opaque
#   geography Point         -> b'\xe6\x10\x00\x00…'   opaque
#
# So SQL_VARIANT needs nothing but permission: the driver decodes the underlying
# value and pyodbc merely refuses to route the type. Handing it straight through
# is a complete fix, not a workaround.
#
# The UDTs are genuinely opaque — SQL Server's own serialisation, not standard
# WKB — so the readable form has to come from the server (`col.ToString()`,
# `col.STAsText()`). Rendering the bytes as hex is the honest stand-in: it does
# not pretend to be a value, and it means no query fails outright. Teaching the
# rewriter to wrap these columns server-side is the real answer and is filed as
# follow-up work.
SQL_VARIANT = -16
SQL_SS_UDT = -151


def passthrough_or_hex(value):
    """Return an already-decoded value untouched; render raw bytes as hex.

    Deliberately does NOT try to parse the bytes. A wrong parse of a geography
    blob would put a plausible, wrong coordinate in front of someone — far worse
    than an obviously-opaque `0xE610…` that says "ask the server to convert
    this". Never raises.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return "0x" + bytes(value).hex().upper()
        except Exception:
            log.warning("could not hex-render a UDT value", exc_info=True)
            return None
    return value


def connect(
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    *,
    timeout_sec: int,
    read_only: bool = False,
    trust_server_cert: bool | None = None,
    multi_subnet_failover: bool | None = None,
):
    """Open a SQL Server connection (autocommit off, so the caller controls
    the transaction like the Postgres path). `timeout_sec` is the per-query
    statement timeout. `read_only=True` adds ApplicationIntent=ReadOnly so an
    AG listener routes to a readable secondary — the executor sets it for
    RO-tier queries and leaves it off for RW/DDL (which must hit the primary).
    Raises if pyodbc / the ODBC driver isn't installed — the caller must have
    gated on engines.is_executable('mssql') first."""
    import pyodbc  # lazy: optional dependency, only on MSSQL-serving hosts

    if trust_server_cert is None:
        trust_server_cert = cfg.get_bool("mssql_trust_server_cert", False)
    if multi_subnet_failover is None:
        multi_subnet_failover = cfg.get_bool("mssql_multi_subnet_failover", True)
    conn = pyodbc.connect(
        odbc_connection_string(
            host, port, database, user, password,
            trust_server_cert=trust_server_cert,
            application_intent=("ReadOnly" if read_only else None),
            multi_subnet_failover=multi_subnet_failover,
        ),
        timeout=_CONNECT_TIMEOUT,
        autocommit=False,
    )
    # Per-query timeout (seconds). pyodbc maps this to SQLSetStmtAttr
    # QUERY_TIMEOUT, the T-SQL analogue of Postgres' statement_timeout.
    conn.timeout = int(timeout_sec)
    # Registered on the CONNECTION, so it covers every cursor and every code
    # path that opens a SQL Server connection through this module — the
    # executor, read-routing discovery, AG topology, connection tests. A
    # per-cursor registration would leave whichever path forgot it still broken.
    conn.add_output_converter(SQL_SS_TIMESTAMPOFFSET, parse_datetimeoffset)
    conn.add_output_converter(SQL_VARIANT, passthrough_or_hex)
    conn.add_output_converter(SQL_SS_UDT, passthrough_or_hex)
    return conn


# Statement execution + result streaming is NOT duplicated here: the executor's
# _run_mssql reuses the cursor-agnostic _execute_main_statement (which streams
# to CSV/XLSX with PII masking + row/size caps) by passing it a pyodbc cursor,
# so SQL Server results go through the exact same shared path as Postgres.


# --- Availability Group topology discovery ---------------------------------
# The target host is an AG LISTENER (a single VNN/VIP that routes to whichever
# replica is primary, and — with ApplicationIntent=ReadOnly — to a readable
# secondary). Given the listener + a login, enumerate the replicas so
# onboarding can confirm the topology + that read-only routing is configured.
# Read-only catalog/DMV query; run it WITHOUT ApplicationIntent so it lands on
# the primary, where the *_states DMVs are fully populated.
_AG_TOPOLOGY_SQL = """
SELECT ag.name                                   AS ag_name,
       ar.replica_server_name                    AS replica,
       ars.role_desc                             AS role,
       ar.availability_mode_desc                 AS availability_mode,
       ar.secondary_role_allow_connections_desc  AS readable_secondary,
       arl.dns_name                              AS listener,
       arl.port                                  AS listener_port
  FROM sys.availability_groups ag
  JOIN sys.availability_replicas ar
       ON ar.group_id = ag.group_id
  JOIN sys.dm_hadr_availability_replica_states ars
       ON ars.replica_id = ar.replica_id
  LEFT JOIN sys.availability_group_listeners arl
       ON arl.group_id = ag.group_id
 ORDER BY ars.role_desc DESC, ar.replica_server_name
"""


def discover_ag_topology(host, port, user, password, *,
                         database: str = "master", timeout_sec: int = 15) -> list[dict]:
    """Enumerate the AG replicas behind a listener, e.g.
    [{ag_name, replica, role, availability_mode, readable_secondary,
      listener, listener_port}]. Connects to the PRIMARY (no ApplicationIntent)
    since the HADR state DMVs are authoritative there. Ready for host-arrival:
    needs a login + pyodbc + the ODBC driver installed."""
    conn = connect(host, port, database, user, password,
                   timeout_sec=timeout_sec, read_only=False)
    try:
        cur = conn.cursor()
        cur.execute(_AG_TOPOLOGY_SQL)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


# --- Bot-DB IP map + read-only routing (no /etc/hosts) ---------------------

def load_host_map() -> dict[str, str]:
    """AG replica server-name → bot-reachable IP, from the bot DB
    (mssql_host_map, migration 069). Empty if the table is missing or empty.
    Keyed by the name as it appears in the AG DMVs (short NetBIOS name)."""
    try:
        rows = db.fetch_all("SELECT server_name, ip FROM mssql_host_map")
    except Exception:
        log.exception("mssql_host_map read failed; treating as empty")
        return {}
    return {r["server_name"].strip().lower(): r["ip"] for r in rows if r.get("ip")}


# Find the current readable secondary's server name from the primary's view.
_RO_SECONDARY_SQL = """
SELECT TOP 1 ar.replica_server_name
  FROM sys.dm_hadr_availability_replica_states s
  JOIN sys.availability_replicas ar ON ar.replica_id = s.replica_id
 WHERE s.is_local = 0
   AND s.role_desc = 'SECONDARY'
   AND ar.secondary_role_allow_connections_desc IN ('ALL', 'READ_ONLY')
 ORDER BY ar.replica_server_name
"""


def resolve_ro_endpoint(listener_host: str, port: int, database: str,
                        user: str, password: str, *, timeout_sec: int = 15):
    """Return (ip, port) of the CURRENT readable secondary for read-only
    routing, resolved via the bot-DB IP map — or None to fall back to the
    primary. Connects to the listener (→ primary), asks the AG DMVs which
    replica is the readable secondary, and maps that server name to an IP in
    mssql_host_map. Does the routing the driver's FQDN redirect would, but
    using the bot DB instead of /etc/hosts, so it survives a host rebuild and
    tracks failover (the secondary is discovered live each time)."""
    host_map = load_host_map()
    if not host_map:
        return None
    try:
        conn = connect(listener_host, port, database, user, password,
                       timeout_sec=timeout_sec, read_only=False)
        try:
            cur = conn.cursor()
            cur.execute(_RO_SECONDARY_SQL)
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        log.exception("MSSQL read-routing discovery failed; falling back to primary")
        return None
    if not row or not row[0]:
        return None
    ip = host_map.get(str(row[0]).strip().lower())
    if not ip:
        log.warning("MSSQL replica %s has no mssql_host_map IP; RO falls back "
                    "to the primary (no read offload).", row[0])
        return None
    return ip, port


# --- Schema catalog (metadata snapshot) ------------------------------------
# schema_catalog.py is engine-dispatched: for a SQL Server target it calls
# these to enumerate databases + read each one's table/column catalog, and
# they return rows shaped EXACTLY like the Postgres reader so schema_catalog's
# shared write phase stores them unchanged. Catalog reads run on the PRIMARY
# (read_only=False, no read-only routing): the primary sees every database
# (incl. non-AG ones a readable secondary wouldn't have), and an hourly
# metadata scan is negligible load.

# User databases the login can actually open. System DBs (master=1, tempdb=2,
# model=3, msdb=4) are excluded — the analogue of hiding rdsadmin/postgres.
_CATALOG_DATABASES_SQL = """
SELECT name
  FROM sys.databases
 WHERE database_id > 4
   AND state_desc = 'ONLINE'
   AND HAS_DBACCESS(name) = 1
 ORDER BY name
"""

# Tables + views in the connected database, with best-effort row count / size
# (views and empty heaps -> NULL). is_ms_shipped filters system objects.
_CATALOG_TABLES_SQL = """
SELECT s.name AS schema_name,
       o.name AS table_name,
       CASE o.type WHEN 'V' THEN 'view' ELSE 'table' END AS relkind,
       sz.row_count AS row_estimate,
       sz.total_bytes AS total_bytes
  FROM sys.objects o
  JOIN sys.schemas s ON s.schema_id = o.schema_id
  OUTER APPLY (
      SELECT SUM(CASE WHEN p.index_id IN (0, 1) THEN p.rows END) AS row_count,
             SUM(a.used_pages) * 8 * 1024                        AS total_bytes
        FROM sys.partitions p
        JOIN sys.allocation_units a ON a.container_id = p.partition_id
       WHERE p.object_id = o.object_id
  ) sz
 WHERE o.type IN ('U', 'V')
   AND o.is_ms_shipped = 0
 ORDER BY s.name, o.name
"""

# Columns for every table/view. data_type carries the length/precision modifier
# (varchar(50), decimal(18,2), nvarchar(max)); is_pk = in a primary key,
# in_index = in ANY index. nvarchar/nchar max_length is bytes → /2 for chars.
_CATALOG_COLUMNS_SQL = """
SELECT s.name         AS schema_name,
       o.name         AS table_name,
       c.column_id    AS ordinal,
       c.name         AS column_name,
       ty.name + CASE
         WHEN ty.name IN ('varchar', 'char', 'varbinary', 'binary')
           THEN '(' + CASE WHEN c.max_length = -1 THEN 'max'
                           ELSE CONVERT(varchar(11), c.max_length) END + ')'
         WHEN ty.name IN ('nvarchar', 'nchar')
           THEN '(' + CASE WHEN c.max_length = -1 THEN 'max'
                           ELSE CONVERT(varchar(11), c.max_length / 2) END + ')'
         WHEN ty.name IN ('decimal', 'numeric')
           THEN '(' + CONVERT(varchar(11), c.precision) + ', '
                    + CONVERT(varchar(11), c.scale) + ')'
         WHEN ty.name IN ('datetime2', 'time', 'datetimeoffset')
           THEN '(' + CONVERT(varchar(11), c.scale) + ')'
         ELSE '' END                                        AS data_type,
       CASE WHEN c.is_nullable = 0 THEN 1 ELSE 0 END        AS not_null,
       object_definition(c.default_object_id)               AS default_expr,
       CASE WHEN EXISTS (SELECT 1 FROM sys.index_columns kc
                           JOIN sys.indexes ki ON ki.object_id = kc.object_id
                                              AND ki.index_id = kc.index_id
                          WHERE kc.object_id = c.object_id
                            AND kc.column_id = c.column_id
                            AND ki.is_primary_key = 1) THEN 1 ELSE 0 END AS is_pk,
       CASE WHEN EXISTS (SELECT 1 FROM sys.index_columns ic
                          WHERE ic.object_id = c.object_id
                            AND ic.column_id = c.column_id) THEN 1 ELSE 0 END AS in_index
  FROM sys.columns c
  JOIN sys.objects o  ON o.object_id = c.object_id
  JOIN sys.schemas s  ON s.schema_id = o.schema_id
  JOIN sys.types ty   ON ty.user_type_id = c.user_type_id
 WHERE o.type IN ('U', 'V')
   AND o.is_ms_shipped = 0
 ORDER BY s.name, o.name, c.column_id
"""

# Per-table index list -> [{name, def}] (def = "(cols) UNIQUE PRIMARY KEY"),
# matching the Postgres reader's jsonb shape. FOR XML PATH concatenates the key
# columns; TYPE + .value() keeps special characters intact.
_CATALOG_INDEXES_SQL = """
SELECT s.name AS schema_name, o.name AS table_name, i.name AS name,
       '(' + STUFF((SELECT ', ' + c2.name
                      FROM sys.index_columns ic2
                      JOIN sys.columns c2 ON c2.object_id = ic2.object_id
                                         AND c2.column_id = ic2.column_id
                     WHERE ic2.object_id = i.object_id
                       AND ic2.index_id = i.index_id
                       AND ic2.is_included_column = 0
                     ORDER BY ic2.key_ordinal
                       FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '')
       + ')' + CASE WHEN i.is_unique = 1 THEN ' UNIQUE' ELSE '' END
             + CASE WHEN i.is_primary_key = 1 THEN ' PRIMARY KEY' ELSE '' END AS def
  FROM sys.indexes i
  JOIN sys.objects o ON o.object_id = i.object_id
  JOIN sys.schemas s ON s.schema_id = o.schema_id
 WHERE o.type = 'U' AND o.is_ms_shipped = 0
   AND i.type > 0 AND i.name IS NOT NULL
 ORDER BY s.name, o.name, i.index_id
"""

# Per-table foreign keys -> [{name, def}] (def = "(cols) -> refschema.reftable").
_CATALOG_FKEYS_SQL = """
SELECT s.name AS schema_name, o.name AS table_name, fk.name AS name,
       '(' + STUFF((SELECT ', ' + pc.name
                      FROM sys.foreign_key_columns fkc
                      JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id
                                         AND pc.column_id = fkc.parent_column_id
                     WHERE fkc.constraint_object_id = fk.object_id
                     ORDER BY fkc.constraint_column_id
                       FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '')
       + ') -> ' + rs.name + '.' + rt.name AS def
  FROM sys.foreign_keys fk
  JOIN sys.objects o   ON o.object_id = fk.parent_object_id
  JOIN sys.schemas s   ON s.schema_id = o.schema_id
  JOIN sys.objects rt  ON rt.object_id = fk.referenced_object_id
  JOIN sys.schemas rs  ON rs.schema_id = rt.schema_id
 WHERE o.is_ms_shipped = 0
 ORDER BY s.name, o.name, fk.name
"""


def _rows(cur) -> list[dict]:
    """pyodbc cursor -> list of dict rows (description[0] is the column name)."""
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def catalog_databases(host, port, database, user, password, *,
                      timeout_sec: int = 15) -> list[str]:
    """User databases the RO login can open on the instance (system DBs
    excluded). Connects to the PRIMARY (no read-only routing) via `database`
    — the target's default DB, which the login is known to reach — and lists
    from sys.databases (visible fleet-wide via the default VIEW ANY DATABASE)."""
    conn = connect(host, port, database, user, password,
                   timeout_sec=timeout_sec, read_only=False)
    try:
        cur = conn.cursor()
        cur.execute(_CATALOG_DATABASES_SQL)
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def catalog_snapshot(host, port, database, user, password, *,
                     timeout_sec: int = 120) -> tuple[list[dict], list[dict]]:
    """Read one database's table + column catalog, returning (tables, columns)
    dicts shaped like schema_catalog's Postgres reader so the shared write
    phase stores them unchanged. Indexes / foreign_keys are [{name, def}] lists
    (or None); SQL Server has no matview / foreign-table / declarative-partition
    concepts we surface, so partition_count / partition_key stay None."""
    from collections import defaultdict
    conn = connect(host, port, database, user, password,
                   timeout_sec=timeout_sec, read_only=False)
    try:
        cur = conn.cursor()
        cur.execute(_CATALOG_TABLES_SQL)
        traw = _rows(cur)
        cur.execute(_CATALOG_COLUMNS_SQL)
        craw = _rows(cur)
        cur.execute(_CATALOG_INDEXES_SQL)
        iraw = _rows(cur)
        cur.execute(_CATALOG_FKEYS_SQL)
        fraw = _rows(cur)
    finally:
        conn.close()

    idx_by: dict = defaultdict(list)
    fk_by: dict = defaultdict(list)
    for r in iraw:
        idx_by[(r["schema_name"], r["table_name"])].append(
            {"name": r["name"], "def": r["def"]})
    for r in fraw:
        fk_by[(r["schema_name"], r["table_name"])].append(
            {"name": r["name"], "def": r["def"]})

    tables = [{
        "schema_name": t["schema_name"],
        "table_name": t["table_name"],
        "relkind": t["relkind"],
        "row_estimate": t["row_estimate"],
        "total_bytes": t["total_bytes"],
        "partition_count": None,
        "partition_key": None,
        "indexes": idx_by.get((t["schema_name"], t["table_name"])) or None,
        "foreign_keys": fk_by.get((t["schema_name"], t["table_name"])) or None,
    } for t in traw]
    columns = [{
        "schema_name": c["schema_name"],
        "table_name": c["table_name"],
        "ordinal": c["ordinal"],
        "column_name": c["column_name"],
        "data_type": c["data_type"],
        "not_null": bool(c["not_null"]),
        "default_expr": c["default_expr"],
        "is_pk": bool(c["is_pk"]),
        "in_index": bool(c["in_index"]),
    } for c in craw]
    return tables, columns
