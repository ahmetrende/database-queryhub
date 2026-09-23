"""ClickHouse: the native protocol, TLS verified, and nothing a readonly user may not send.

Measured against ClickHouse Cloud (26.4) before any of this was written, as the
read-only login QueryHub uses there:

* **The login is `readonly=1`, and readonly=1 refuses every setting change** --
  a SETTINGS clause, a per-query setting, a session setting. `Code 164: Cannot
  modify 'max_block_size' setting in readonly mode`. The driver's DB-API cursor
  sends `max_block_size` the moment it is asked to stream, so it is not used:
  `ClickHouseCursor` drives `Client.execute_iter` directly and sends no settings
  at all.
* **The server enforces no limits for that login** -- `max_execution_time`,
  `max_result_rows` and `max_memory_usage` are all 0, and readonly=1 forbids
  setting them from here. So the time limit is ours: a watchdog closes the
  connection at the deadline. On the native protocol a closed connection
  cancels the query on the server; over HTTP a readonly query would keep
  running (that needs `cancel_http_readonly_queries_on_client_close`, a setting
  this login cannot send). That difference is why this is native, port 9440.
* **Streaming works and stops early.** A 10^8-row SELECT gave up its first ten
  rows in 0.05 s and ended when the connection closed, so the row cap is
  enforced by reading no further, not by materialising the result first.
* **A service can sleep.** Some bill compute when woken and every query resets
  the idle timer, so nothing here polls; the schema catalog decides when a read
  is worth it (see `refresh_schema_catalog.py`).

The login may read `system.*`; QueryHub still refuses those schemas for user
queries (EngineSpec.blocked_schemas). The catalog queries below read
`system.databases/tables/columns` because that is the server's own catalog,
the way the Postgres path reads pg_catalog.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
import uuid

log = logging.getLogger(__name__)

# How often the watchdog wakes, and how often of those it asks the bot DB
# whether somebody pressed Stop. A cancel lands within ~2 s, and a query costs
# at most one small read every two seconds while it runs.
_WATCH_TICK_SEC = 1.0
_CANCEL_POLL_EVERY = 2

# Databases a user never queries and the catalog never lists.
HIDDEN_DATABASES = ("system", "information_schema", "INFORMATION_SCHEMA")


class ClickHouseRefused(RuntimeError):
    """The server refused or failed the statement; the message is its own,
    without the stack trace it sends with it."""


def server_message(e: Exception) -> str:
    """`Code 60: Unknown table expression identifier 'x'` from a driver
    ServerException, whose text is a `Code: N.` line, the message after
    `DB::Exception:`, and a stack trace of twenty frames. The error scrubber
    keeps a first line, and the first line here is `Code: 60.` alone."""
    code = getattr(e, "code", None)
    text = getattr(e, "message", None) or str(e)
    m = re.search(r"DB::Exception:\s*(.*?)(?:\.\s*Stack trace:|\s*Stack trace:|$)",
                  text, re.S)
    msg = " ".join((m.group(1) if m else text).split())
    if msg and not msg.endswith("."):
        msg += "."
    return f"Code {code}: {msg}" if code is not None else msg


class ClickHouseStopped(RuntimeError):
    """The watchdog closed the connection: the deadline passed or a user
    pressed Stop. The message says which, in words a requester can act on."""


def client(host: str, port: int, database: str | None, user: str, password: str,
           *, timeout_sec: int, client_name: str = "queryhub"):
    """A native-protocol client with TLS verified against the public CA bundle.

    `settings` is deliberately absent: the readonly login refuses any, and the
    driver sends none unless it is given some.
    """
    import certifi
    from clickhouse_driver import Client
    return Client(
        host=host, port=port, database=database or "default",
        user=user, password=password,
        secure=True, verify=True, ca_certs=certifi.where(),
        connect_timeout=10,
        # Per socket read, not per query: the first block of a heavy query can
        # take this long to arrive. The watchdog bounds the whole query.
        send_receive_timeout=max(10, int(timeout_sec)),
        client_name=client_name,
    )


_DT64_RE = re.compile(r"DateTime64\((\d+)")


class _Column(tuple):
    """A DB-API description entry that answers `type_display` with the
    engine's own type name, and carries a DateTime64's precision as the scale.

    `executor._column_types` reads `type_display` for the grid's header, and
    `cell_format.sub_second_digits` reads index 5 so a `DateTime64(3)` value is
    written with three sub-second digits rather than Python's six."""

    __slots__ = ()

    def __new__(cls, name: str, type_name: str):
        m = _DT64_RE.search(type_name or "")
        scale = int(m.group(1)) if m else (0 if "DateTime" in (type_name or "") else None)
        return super().__new__(cls, (name, type_name, None, None, None, scale, None))

    @property
    def type_display(self) -> str:
        return self[1]


class ClickHouseCursor:
    """One connection, one statement at a time, in the DB-API shape the
    executor's shared statement runner reads: `execute`, `description`,
    `rowcount`, and iteration over rows.

    The watchdog starts with each statement and ends with it. It closes the
    socket when the deadline passes or `is_cancelled()` turns true; the read in
    progress then fails, and `__iter__` reports why instead of a socket error.
    """

    arraysize = 1

    def __init__(self, host: str, port: int, database: str | None, user: str,
                 password: str, *, timeout_sec: int = 300,
                 request_id: int | None = None, on_started=None,
                 is_cancelled=None):
        self._client = client(host, port, database, user, password,
                              timeout_sec=timeout_sec,
                              client_name=f"queryhub req={request_id}")
        self._timeout = int(timeout_sec)
        self._request_id = request_id
        self._on_started = on_started
        self._is_cancelled = is_cancelled
        self.description = None
        self.rowcount = -1
        self.query_id: str | None = None
        self._rows = None
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._why: str | None = None

    # -- DB-API surface ----------------------------------------------------

    def execute(self, sql: str, *_args, **_kwargs) -> "ClickHouseCursor":
        self._end_watch()
        self.query_id = f"queryhub-{self._request_id}-{uuid.uuid4().hex[:12]}"
        if self._on_started is not None:
            try:
                self._on_started(self.query_id)
            except Exception:
                log.warning("clickhouse: could not record the query id for "
                            "request %s; Stop from another process will rely "
                            "on the watchdog alone", self._request_id,
                            exc_info=True)
        self._why = None
        self._start_watch()
        try:
            it = self._client.execute_iter(sql, with_column_types=True,
                                           query_id=self.query_id)
            columns = next(it, None) or []
        except Exception as e:
            raise self._reason(e) from e
        self.description = [_Column(n, t) for n, t in columns] or None
        self._rows = it
        return self

    def __iter__(self):
        if self._rows is None:
            return
        try:
            for row in self._rows:
                yield row
        except Exception as e:
            raise self._reason(e) from e
        finally:
            self._end_watch()

    def fetchmany(self, size: int | None = None) -> list:
        """Straight from the stream: going through `__iter__` would start a
        generator per call, and each one ends the watchdog when it is dropped."""
        out = []
        if self._rows is None:
            return out
        try:
            for row in self._rows:
                out.append(row)
                if len(out) >= (size or self.arraysize):
                    break
        except Exception as e:
            raise self._reason(e) from e
        return out

    def close(self) -> None:
        """Disconnect. Also what cancels a result the caller stopped reading
        at the row cap: the server ends the query when the connection goes."""
        self._end_watch()
        try:
            self._client.disconnect()
        except Exception:
            pass

    # -- the watchdog --------------------------------------------------------

    def _start_watch(self) -> None:
        self._stop.clear()
        deadline = time.monotonic() + self._timeout

        def watch():
            ticks = 0
            while not self._stop.wait(_WATCH_TICK_SEC):
                ticks += 1
                if time.monotonic() >= deadline:
                    self._why = "timeout"
                elif (self._is_cancelled is not None
                      and ticks % _CANCEL_POLL_EVERY == 0):
                    try:
                        if self._is_cancelled():
                            self._why = "cancelled"
                    except Exception:
                        log.debug("clickhouse: cancel poll failed", exc_info=True)
                if self._why:
                    self._break_connection()
                    return

        self._watchdog = threading.Thread(target=watch, daemon=True,
                                          name=f"ch-watch-{self._request_id}")
        self._watchdog.start()

    def _end_watch(self) -> None:
        self._stop.set()
        w, self._watchdog = self._watchdog, None
        if w is not None and w is not threading.current_thread():
            w.join(timeout=2)

    def _break_connection(self) -> None:
        """Shut the socket under the blocked read. Shutdown rather than close
        from this thread: the reading thread still owns the connection object
        and tears it down itself in `close()`."""
        conn = getattr(self._client, "connection", None)
        sock = getattr(conn, "socket", None)
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
            else:
                self._client.disconnect()
        except Exception:
            log.debug("clickhouse: breaking the connection failed", exc_info=True)

    def _reason(self, e: Exception) -> Exception:
        typ = _unreadable_type(e)
        if typ:
            if typ.startswith(("AggregateFunction", "SimpleAggregateFunction")):
                return ValueError(
                    f"A column holds an aggregate STATE ({typ}), which is "
                    f"stored unfinished and cannot be read as a value. Select "
                    f"it through its -Merge combinator (e.g. argMaxMerge(col)) "
                    f"or finalizeAggregation(col).")
            return ValueError(
                f"A column's type ({typ}) cannot be read by QueryHub's "
                f"ClickHouse driver. Select it as toString(col).")
        if self._why is None and type(e).__name__ == "ServerException":
            return ClickHouseRefused(server_message(e))
        if self._why == "timeout":
            return ClickHouseStopped(
                f"The query ran past the {self._timeout}s limit and was "
                f"stopped. Narrow the WHERE clause, add a LIMIT, or ask the "
                f"DBA team to raise the limit.")
        if self._why == "cancelled":
            return ClickHouseStopped("Cancelled while it was running.")
        return e


def _unreadable_type(e: Exception) -> str | None:
    """The type named by the driver's `Unknown type ...` error, if that is
    what this is. The driver decodes values itself and has no reader for an
    aggregate state or for some newer types; the server sends them fine."""
    name = type(e).__name__
    if name != "UnknownTypeError":
        return None
    m = re.search(r"Unknown type (.+)$", str(e).strip().splitlines()[-1])
    return m.group(1).strip() if m else "unknown"


# -- catalog -------------------------------------------------------------------

def _rows(c, sql: str, params: dict | None = None) -> list[dict]:
    data, cols = c.execute(sql, params or {}, with_column_types=True)
    names = [n for n, _t in cols]
    return [dict(zip(names, r)) for r in data]


def catalog_databases(host, port, user, password, *, timeout_sec: int = 30) -> list[str]:
    c = client(host, port, "default", user, password, timeout_sec=timeout_sec,
               client_name="queryhub-schema-snapshot")
    try:
        return [r["name"] for r in _rows(
            c, "SELECT name FROM system.databases "
               "WHERE name NOT IN %(hidden)s ORDER BY name",
            {"hidden": HIDDEN_DATABASES})]
    finally:
        c.disconnect()


def catalog_snapshot(host, port, database, user, password, *,
                     timeout_sec: int = 60) -> tuple[list[dict], list[dict]]:
    """(tables, columns) in the shape schema_catalog stores for every engine.

    ClickHouse has no schema level inside a database, so the database name is
    the schema -- a table reads `ledger.trades`, which is how a query names it.
    The sorting key is what ClickHouse has in place of an index; it is listed as
    the one "index" so the browser shows what a query is fast on.
    """
    c = client(host, port, database, user, password, timeout_sec=timeout_sec,
               client_name="queryhub-schema-snapshot")
    try:
        raw = _rows(c, """
            SELECT database AS schema_name, name AS table_name, engine,
                   total_rows, total_bytes, partition_key, sorting_key
              FROM system.tables
             WHERE database = %(db)s AND NOT is_temporary
             ORDER BY name""", {"db": database})
        columns = _rows(c, """
            SELECT database AS schema_name, table AS table_name,
                   position AS ordinal, name AS column_name, type AS data_type,
                   NOT startsWith(type, 'Nullable(') AS not_null,
                   nullIf(default_expression, '') AS default_expr,
                   is_in_primary_key = 1 AS is_pk,
                   (is_in_sorting_key = 1 OR is_in_primary_key = 1) AS in_index
              FROM system.columns
             WHERE database = %(db)s
             ORDER BY table, position""", {"db": database})
    finally:
        c.disconnect()
    # ClickHouse answers a comparison with UInt8 0/1, and the bot DB stores
    # these as boolean: a list of ints reaches it as smallint[] and the cast to
    # boolean[] fails (measured on the first live snapshot).
    for col in columns:
        for key in ("not_null", "is_pk", "in_index"):
            col[key] = bool(col[key])
    tables = [{
        "schema_name": t["schema_name"], "table_name": t["table_name"],
        "relkind": _relkind(t["engine"]),
        "row_estimate": t["total_rows"], "total_bytes": t["total_bytes"],
        "partition_count": None,
        "partition_key": t["partition_key"] or None,
        "indexes": ([{"name": "sorting key", "def": f"ORDER BY ({t['sorting_key']})"}]
                    if t["sorting_key"] else None),
        "foreign_keys": None,
    } for t in raw]
    return tables, columns


def _relkind(engine: str) -> str:
    """The catalog's relkind for a ClickHouse table engine."""
    if engine in ("View", "LiveView", "WindowView"):
        return "v"
    if engine == "MaterializedView":
        return "m"
    return "r"


def probe(host, port, database, user, password, *, timeout_sec: int = 10) -> str | None:
    """The server version, over the same connection the executor opens."""
    c = client(host, port, database, user, password, timeout_sec=timeout_sec,
               client_name="queryhub-connection-test")
    try:
        rows = c.execute("SELECT version()")
        return rows[0][0] if rows else None
    finally:
        c.disconnect()


def kill_query(host, port, user, password, query_id: str) -> bool:
    """Ask the server to stop a query by id. True if a replica reported it.

    Best effort, and not the mechanism a cancel relies on: a ClickHouse Cloud
    service has several replicas and this connection may land on a different
    one from the query. The watchdog in the executing process is what stops it.
    """
    c = client(host, port, "default", user, password, timeout_sec=10,
               client_name="queryhub-cancel")
    try:
        rows = c.execute("KILL QUERY WHERE query_id = %(qid)s ASYNC",
                         {"qid": query_id})
        return bool(rows)
    finally:
        c.disconnect()
