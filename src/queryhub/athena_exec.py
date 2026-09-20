"""Amazon Athena: catalog reads (Glue), and the assumed-role session behind them.

The first target that is not a server. There is no host to open a socket to, no
username, and no password anywhere in this database: the gateway's own instance
role ASSUMES a read-only role in the account that owns the archive, and every
call -- Glue, Athena, S3 -- goes out under that session. What identifies the
target is data, not a connection: region, workgroup, catalog, Glue database and
the role to assume, all carried per target in `target_servers.engine_config`
(migration 127) so a second archive needs a row and no code.

This module holds the CATALOG half. Execution (StartQueryExecution, polling,
paged results) lands with the engine's wiring; `engines.WIRED_ENGINES` keeps a
tagged target fail-closed until then.

Two deliberate shapes:

* **The schema comes from Glue, not from SQL.** `information_schema` would mean
  running a query -- billable, slower, and refused by the engine's own safety
  profile. Glue answers for free and is the same catalog Athena reads.
* **A Glue database maps onto BOTH of QueryHub's middle levels.** The product
  models target -> database -> schema -> table; Athena has only
  database -> table. Putting the database name in `schema_name` as well is what
  makes the generated SQL right: the browser qualifies as `schema.table`, and
  `archive_db.events` is exactly how the archive is addressed.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

# One assumed-role session per (role, region), reused until shortly before it
# expires. The role's max session duration is an hour and a query is bounded by
# `query_timeout_sec` (300s), so nothing long-running ever straddles a refresh;
# this cache exists to keep an hourly catalog sweep from calling STS per table.
_SESSIONS: dict[tuple, tuple[float, object]] = {}
_LOCK = threading.Lock()
_SESSION_SECONDS = 3600
_REFRESH_MARGIN = 300          # renew 5 minutes early rather than race expiry


class AthenaConfigError(RuntimeError):
    """`engine_config` is missing something this target cannot work without.
    Raised as a configuration error, never as a connection error: the fix is a
    row in the database, not a retry."""


def config_of(target) -> dict:
    """The target's Athena settings, with every required key present.

    Fails loudly and by name. A half-filled config that reached a boto3 call
    would surface as an opaque SDK error naming none of the four things an
    operator has to fix."""
    cfg = dict(getattr(target, "engine_config", None) or {})
    missing = [k for k in ("region", "workgroup", "database", "role_arn")
               if not cfg.get(k)]
    if missing:
        raise AthenaConfigError(
            f"target {getattr(target, 'alias', '?')}: engine_config is missing "
            f"{', '.join(missing)}")
    cfg.setdefault("catalog", "AwsDataCatalog")
    return cfg


def session(cfg: dict):
    """A boto3 Session under the target's assumed role.

    Credentials come from the gateway's own chain (instance role), which is the
    whole point: there is no secret for this target in the metadata database,
    so there is none to leak, rotate or forget."""
    import boto3

    key = (cfg["role_arn"], cfg["region"])
    now = time.time()
    with _LOCK:
        cached = _SESSIONS.get(key)
        if cached and cached[0] - _REFRESH_MARGIN > now:
            return cached[1]

    sts = boto3.client("sts", region_name=cfg["region"])
    resp = sts.assume_role(RoleArn=cfg["role_arn"],
                           RoleSessionName="queryhub",
                           DurationSeconds=_SESSION_SECONDS)
    c = resp["Credentials"]
    sess = boto3.Session(aws_access_key_id=c["AccessKeyId"],
                         aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"],
                         region_name=cfg["region"])
    with _LOCK:
        _SESSIONS[key] = (c["Expiration"].timestamp(), sess)
    return sess


def _glue(cfg: dict):
    return session(cfg).client("glue", region_name=cfg["region"])


def catalog_databases(cfg: dict) -> list[str]:
    """Glue databases in this catalog.

    The fleet-wide picker calls this; the target's own `database` is the one it
    defaults to. Listing the rest costs one call and lets a second archive in
    the same catalog appear without a code change."""
    # No CatalogId: Glue defaults to the account the session is in, which is
    # the account that owns the archive because that is whose role we assumed.
    # A cross-ACCOUNT Glue catalog would need one, and would also need a second
    # trust relationship -- a different problem than this line.
    out: list[str] = []
    for page in _glue(cfg).get_paginator("get_databases").paginate():
        out.extend(d["Name"] for d in page.get("DatabaseList", []))
    return sorted(out)


def _table_rows(t: dict, database: str) -> tuple[dict, list[dict]]:
    """One Glue table -> the (table, columns) shapes schema_catalog stores.

    Partition keys are included AS COLUMNS. In Athena they are queryable like
    any other column -- `WHERE month = '2026-05'` is the single most important
    predicate on this archive, because it is what stops a query reading every
    other month -- so leaving them out of the catalog would hide the one column
    the schema browser most needs to offer."""
    sd = t.get("StorageDescriptor") or {}
    params = t.get("Parameters") or {}
    part_keys = t.get("PartitionKeys") or []

    def _int(name):
        try:
            return int(params[name])
        except (KeyError, TypeError, ValueError):
            return None

    table = {
        "schema_name": database,          # see the module docstring
        "table_name": t["Name"],
        # Glue's own vocabulary, mapped onto the relkind letters the rest of
        # the product already speaks.
        "relkind": "v" if t.get("TableType") == "VIRTUAL_VIEW" else "r",
        # The crawler's estimates. Absent on a table it has not measured, and
        # None is the honest answer there -- a zero would read as "empty".
        "row_estimate": _int("recordCount"),
        "total_bytes": _int("sizeKey"),
        "partition_count": None,          # filled by the caller when counted
        "partition_key": (", ".join(k["Name"] for k in part_keys) or None),
        # Glue has neither, and an empty list would claim we looked and found
        # none of something this catalog cannot hold.
        "indexes": None,
        "foreign_keys": None,
    }

    columns = []
    for i, c in enumerate(list(sd.get("Columns") or []) + list(part_keys), start=1):
        columns.append({
            "schema_name": database,
            "table_name": t["Name"],
            "ordinal": i,
            "column_name": c["Name"],
            "data_type": c.get("Type") or "string",
            # Glue records no nullability, no defaults, no keys. False is not
            # a claim that the column is nullable -- it is the absence of a
            # claim, which is all this catalog can honestly report.
            "not_null": False,
            "default_expr": None,
            "is_pk": False,
            "in_index": False,
        })
    return table, columns


def catalog_snapshot(cfg: dict, database: str,
                     count_partitions: bool = True) -> tuple[list[dict], list[dict]]:
    """One Glue database's tables + columns, shaped like the Postgres reader's
    output so the shared write phase stores them unchanged."""
    glue = _glue(cfg)
    tables: list[dict] = []
    columns: list[dict] = []
    for page in glue.get_paginator("get_tables").paginate(DatabaseName=database):
        for t in page.get("TableList", []):
            table, cols = _table_rows(t, database)
            if count_partitions and (t.get("PartitionKeys") or []):
                table["partition_count"] = _count_partitions(glue, database, t["Name"])
            tables.append(table)
            columns.extend(cols)
    return tables, columns


def _count_partitions(glue, database: str, table: str) -> int | None:
    """How many partitions the table has, or None if we could not tell.

    Non-fatal by construction: a partition count is a nicety on a screen, and
    losing it must never cost the tables-and-columns snapshot that the schema
    browser actually needs. Capped, because a table with a partition per hour
    would otherwise page forever for one number nobody reads that closely."""
    seen, cap = 0, 10_000
    try:
        for page in glue.get_paginator("get_partitions").paginate(
                DatabaseName=database, TableName=table,
                PaginationConfig={"PageSize": 1000}):
            seen += len(page.get("Partitions", []))
            if seen >= cap:
                return seen
        return seen
    except Exception:
        log.info("athena: partition count unavailable for %s.%s",
                 database, table, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Execution: a DB-API-shaped cursor over the Athena API.
# ---------------------------------------------------------------------------
#
# Athena is asynchronous -- start, poll, page the results -- and the executor
# is written against a cursor. Rather than teach the executor a second shape,
# this adapts Athena to the shape it already speaks: `execute()`,
# `description`, `rowcount`, iteration. The masking, the row limit, the CSV and
# XLSX writers, the statement guard and the completion path then work here
# unchanged, exactly as they do for the SQL Server path.
#
# Three things this deliberately does NOT do:
#
#   * **No `ResultConfiguration`.** The workgroup enforces its own output
#     location; sending one would be ignored at best and a second copy of
#     financial data in a bucket nobody governs at worst.
#   * **No `ResultReuseConfiguration`.** Athena can return a previous run's
#     result for up to 7 days. It is off by default and MUST stay off: a reused
#     result reports `DataScannedInBytes = 0`, so the audit trail would record
#     that a query scanned nothing when it scanned gigabytes the first time.
#     Cheaper queries are not worth an audit record that lies.
#   * **No numeric parsing.** Every value arrives as a string and is kept as
#     one. The money columns here are `decimal(38,18)` -- 19 significant digits,
#     where a float carries 15-16 -- so parsing would silently round the last
#     digits and equality comparisons would stop matching. The CSV is written
#     from the string the service returned.

_POLL_FIRST = 0.2      # most archive queries finish in a couple of seconds
_POLL_MAX = 1.0
_PAGE_SIZE = 1000      # GetQueryResults' maximum


class AthenaQueryError(RuntimeError):
    """The service refused or failed the query. Carries the reason Athena gave
    -- including the one that matters most here, the workgroup's scanned-bytes
    limit, which reads as `Bytes scanned limit was exceeded`."""


class _Column(tuple):
    """A DB-API description entry that also answers `type_display`.

    `executor._column_types` reads that attribute first and falls back to a
    Python type object. Giving it the engine's own type name means the grid's
    header tooltip says `decimal(38,18)` rather than `str`, which on this
    target is the difference between a number a reader trusts and one they
    should not."""

    __slots__ = ()

    def __new__(cls, name: str, type_name: str):
        return super().__new__(cls, (name, type_name, None, None, None, None, None))

    @property
    def type_display(self) -> str:
        return self[1]


class AthenaCursor:
    """One query. Not reusable, not thread-safe, and not pooled -- there is no
    connection to pool: every call is an HTTPS request under the target's
    assumed role."""

    arraysize = 1

    def __init__(self, cfg: dict, *, timeout_sec: int = 300,
                 request_id: int | None = None, on_started=None):
        self._cfg = cfg
        self._timeout = timeout_sec
        self._request_id = request_id
        # Called with the execution id the instant the service returns it, so
        # the caller can persist it BEFORE the poll loop starts. A cancel that
        # arrives while the query is running has nothing else to aim at.
        self._on_started = on_started
        self._client = session(cfg).client("athena", region_name=cfg["region"])
        self.execution_id: str | None = None
        self.stats: dict = {}
        self.description = None
        self.rowcount = -1
        self._first_page: dict | None = None

    # -- DB-API surface ----------------------------------------------------

    def execute(self, sql: str, *_args, **_kwargs) -> "AthenaCursor":
        start = self._client.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": self._cfg["database"],
                                   "Catalog": self._cfg.get("catalog",
                                                            "AwsDataCatalog")},
            WorkGroup=self._cfg["workgroup"],
        )
        self.execution_id = start["QueryExecutionId"]
        if self._on_started is not None:
            try:
                self._on_started(self.execution_id)
            except Exception:
                log.warning("athena: could not record the execution id for "
                            "request %s — the query runs, but a cancel will "
                            "have nothing to aim at", self._request_id,
                            exc_info=True)
        self._await_completion()
        self._load_first_page()
        return self

    def __iter__(self):
        """Rows in column order, as tuples of strings (None for NULL)."""
        page = self._first_page
        if page is None:
            return
        while True:
            rows = page["ResultSet"]["Rows"]
            for row in rows:
                yield tuple(cell.get("VarCharValue") for cell in row["Data"])
            token = page.get("NextToken")
            if not token:
                return
            page = self._client.get_query_results(
                QueryExecutionId=self.execution_id,
                NextToken=token, MaxResults=_PAGE_SIZE)

    def close(self) -> None:
        """Stops the query if it is somehow still running. Safe to call twice:
        stopping a finished query is a no-op the service accepts."""
        if self.execution_id:
            try:
                self._client.stop_query_execution(
                    QueryExecutionId=self.execution_id)
            except Exception:
                pass

    def cancel(self) -> None:
        if self.execution_id:
            self._client.stop_query_execution(QueryExecutionId=self.execution_id)

    # -- the asynchronous half --------------------------------------------

    def _await_completion(self) -> None:
        deadline = time.monotonic() + self._timeout
        wait = _POLL_FIRST
        while True:
            info = self._client.get_query_execution(
                QueryExecutionId=self.execution_id)["QueryExecution"]
            state = info["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                self.stats = dict(info.get("Statistics") or {})
                if state == "SUCCEEDED":
                    return
                reason = (info["Status"].get("StateChangeReason")
                          or f"query {state.lower()}")
                raise AthenaQueryError(reason)
            if time.monotonic() >= deadline:
                # Stop it rather than merely stop waiting: an abandoned query
                # keeps scanning, and scanning is what this engine bills for.
                self.cancel()
                raise TimeoutError(
                    f"Athena query exceeded {self._timeout}s and was stopped")
            time.sleep(wait)
            wait = min(wait * 2, _POLL_MAX)

    def _load_first_page(self) -> None:
        """The first page also carries the column metadata -- and, for a
        SELECT, a header row that repeats the column names as data. Dropping it
        is not cosmetic: left in, every result would carry a first row of
        column labels, and a CSV consumer would read them as values."""
        page = self._client.get_query_results(
            QueryExecutionId=self.execution_id, MaxResults=_PAGE_SIZE)
        meta = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
        self.description = [_Column(c["Name"], _type_label(c)) for c in meta]
        rows = page["ResultSet"]["Rows"]
        if rows:
            first = [cell.get("VarCharValue") for cell in rows[0]["Data"]]
            if first == [c["Name"] for c in meta]:
                page["ResultSet"]["Rows"] = rows[1:]
        self._first_page = page


def _type_label(col: dict) -> str:
    """`decimal(38,18)`, `varchar`, `timestamp` — the engine's own name, with
    the precision that makes a decimal readable as a decimal."""
    name = col.get("Type") or "varchar"
    if name == "decimal":
        precision, scale = col.get("Precision"), col.get("Scale")
        if precision is not None and scale is not None:
            return f"decimal({precision},{scale})"
    return name
