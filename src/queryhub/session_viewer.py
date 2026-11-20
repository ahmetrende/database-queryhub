"""Read-only session and lock viewer for one target database.

A super-admin opens it from the web admin panel to see who is connected, what
runs, what waits and who blocks whom. It reads the engine's own activity views
with the target's READ-ONLY credential. It never kills or cancels a session.

Both engines return the same row shape, so the client renders one table:

    sessions: {id, user, app, client, state, wait, started, queryStarted,
               blockedBy, blocking, database, query, queryHidden}
    locks:    {id, type, mode, granted, relation}

Query text is other people's SQL. It can hold literal values, so it goes
through the PII value scan when masking is on. A masking error hides the text
instead of showing it unmasked.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from . import config as cfg
from . import pii, targets

log = logging.getLogger(__name__)

APP_NAME = "queryhub-session-viewer"
TIMEOUT_SEC = 10
MAX_SESSIONS = 500
MAX_LOCKS = 2000
QUERY_CHARS = 2000

NOTE_PG_NO_STATS = ("The read-only login cannot read other sessions' query text. "
                    "Grant pg_read_all_stats to it.")
NOTE_MSSQL_OWN_ONLY = ("The read-only login sees only its own session. "
                       "Grant VIEW SERVER STATE to it.")
UNSUPPORTED = "Sessions are not available for this engine."

# What PostgreSQL puts in pg_stat_activity.query for a session that the
# caller's role may not inspect.
_PG_HIDDEN_TEXT = "<insufficient privilege>"

PG_SESSIONS_SQL = (
    "SELECT pid, usename, application_name, client_addr::text AS client_addr, "
    "       state, backend_type, backend_start, xact_start, query_start, "
    "       state_change, wait_event_type, wait_event, "
    "       pg_blocking_pids(pid) AS blocked_by, datname, "
    f"      left(query, {QUERY_CHARS}) AS query "
    "  FROM pg_stat_activity "
    " WHERE pid <> pg_backend_pid() "
    " ORDER BY query_start NULLS LAST "
    f" LIMIT {MAX_SESSIONS}"
)

PG_LOCKS_SQL = (
    "SELECT l.pid, l.locktype, l.mode, l.granted, "
    "       CASE WHEN l.relation IS NOT NULL "
    "            THEN l.relation::regclass::text END AS relation, "
    "       l.transactionid::text AS xid "
    "  FROM pg_locks l "
    " WHERE l.pid <> pg_backend_pid() "
    "   AND (l.database IS NULL OR l.database = "
    "        (SELECT oid FROM pg_database WHERE datname = current_database())) "
    f" LIMIT {MAX_LOCKS}"
)

# Without VIEW SERVER STATE the DMVs show the caller's own session only. The
# viewer leaves its own session out, so an empty list would look like an idle
# server. This check tells the two cases apart.
MSSQL_PERMISSION_SQL = (
    "SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE') AS has_state"
)

MSSQL_SESSIONS_SQL = (
    f"SELECT TOP {MAX_SESSIONS} "
    "       s.session_id, s.login_name, s.program_name, s.host_name, "
    "       s.status, s.login_time, r.command, r.wait_type, r.wait_time, "
    "       r.blocking_session_id, r.start_time, s.last_request_start_time, "
    "       DB_NAME(s.database_id) AS database_name, "
    f"      LEFT(t.text, {QUERY_CHARS}) AS query_text "
    "  FROM sys.dm_exec_sessions s "
    "  LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id "
    " OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t "
    " WHERE s.is_user_process = 1 AND s.session_id <> @@SPID "
    " ORDER BY COALESCE(r.start_time, s.last_request_start_time)"
)

MSSQL_LOCKS_SQL = (
    f"SELECT TOP {MAX_LOCKS} "
    "       l.request_session_id, l.resource_type, l.request_mode, "
    "       l.request_status, "
    "       CASE WHEN l.resource_type = 'OBJECT' "
    "            THEN OBJECT_SCHEMA_NAME(l.resource_associated_entity_id) + '.' "
    "                 + OBJECT_NAME(l.resource_associated_entity_id) END "
    "            AS relation "
    "  FROM sys.dm_tran_locks l "
    " WHERE l.resource_database_id = DB_ID() AND l.request_session_id <> @@SPID"
)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def _connect_pg(target: Any, user: str, password: str, database: str) -> Any:
    """The schema_catalog._connect pattern with the RO login and a short
    timeout. default_transaction_read_only makes even a mistaken statement in
    this module unable to write."""
    import psycopg

    return psycopg.connect(
        host=target.host,
        port=target.port,
        dbname=database,
        user=user,
        password=password,
        connect_timeout=8,
        **cfg.target_ssl_kwargs(target.host),
        application_name=APP_NAME,
        options=(f"-c statement_timeout={TIMEOUT_SEC * 1000} "
                 "-c default_transaction_read_only=on"),
    )


def _connect_mssql(target: Any, user: str, password: str, database: str) -> Any:
    """read_only=False on purpose. read_only=True adds ApplicationIntent=ReadOnly,
    and an AG listener then routes the login to a readable secondary. The DMVs
    there list the secondary's sessions, not the primary's, so the operator
    would see the wrong server. The login is still the RO login, so its own
    grants keep it from writing."""
    from . import mssql_exec

    return mssql_exec.connect(
        target.host, target.port, database, user, password,
        timeout_sec=TIMEOUT_SEC, read_only=False, app_name=APP_NAME)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _column_names(description: Sequence[Any]) -> list[str]:
    # psycopg gives Column objects with .name, pyodbc gives plain tuples.
    return [getattr(d, "name", None) or d[0] for d in description]


def _rows(cur: Any, sql: str) -> list[dict[str, Any]]:
    cur.execute(sql)
    names = _column_names(cur.description or [])
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # SQL Server returns local server time without an offset. It is
            # passed on as is: inventing a zone would make it look exact.
            return value.isoformat()
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _mask_query(text: str | None) -> tuple[str | None, bool]:
    """Return (text, hidden). Fail closed: any error hides the text."""
    if text is None:
        return None, False
    try:
        if not pii.is_enabled():
            return text, False
        masked = pii.mask_value(text, set())
        if not isinstance(masked, str):
            return None, True
        return masked, False
    except Exception:  # noqa: BLE001 - a masking failure must not leak the text
        log.warning("session viewer: query text masking failed, text hidden")
        return None, True


def _int_list(values: Iterable[Any] | None) -> list[int]:
    out: list[int] = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _add_blocking(sessions: list[dict[str, Any]]) -> None:
    """Fill `blocking`: the sessions that name this one in their blockedBy."""
    by_id: dict[int, list[int]] = {}
    for s in sessions:
        for blocker in s["blockedBy"]:
            by_id.setdefault(blocker, []).append(s["id"])
    for s in sessions:
        s["blocking"] = sorted(by_id.get(s["id"], []))


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def pg_session(row: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """One pg_stat_activity row in the shared shape. The second value is True
    when PostgreSQL hid the query text from this login."""
    raw = row.get("query")
    no_privilege = raw == _PG_HIDDEN_TEXT
    if no_privilege:
        query, hidden = None, True
    else:
        query, hidden = _mask_query(raw)
    wait = None
    if row.get("wait_event_type") or row.get("wait_event"):
        wait = ":".join(str(p) for p in (row.get("wait_event_type"),
                                          row.get("wait_event")) if p)
    return {
        "id": int(row["pid"]),
        "user": row.get("usename"),
        "app": row.get("application_name") or None,
        "client": row.get("client_addr"),
        # A background process (autovacuum, walwriter) has no state. Its
        # backend type is the useful label for it.
        "state": row.get("state") or row.get("backend_type"),
        "wait": wait,
        "started": _iso(row.get("backend_start")),
        "queryStarted": _iso(row.get("query_start")),
        "blockedBy": _int_list(row.get("blocked_by")),
        "database": row.get("datname"),
        "query": query,
        "queryHidden": hidden,
    }, no_privilege


def pg_lock(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["pid"]) if row.get("pid") is not None else None,
        "type": row.get("locktype"),
        "mode": row.get("mode"),
        "granted": bool(row.get("granted")),
        "relation": row.get("relation"),
    }


def mssql_session(row: Mapping[str, Any]) -> dict[str, Any]:
    query, hidden = _mask_query(row.get("query_text"))
    wait = row.get("wait_type")
    if wait and row.get("wait_time") is not None:
        wait = f"{wait} ({row['wait_time']} ms)"
    blocker = row.get("blocking_session_id")
    # 0 means not blocked. Negative ids (-2, -3, -4) are orphaned or latch
    # owners, not sessions, so they cannot be linked to a row.
    blocked_by = [int(blocker)] if blocker is not None and int(blocker) > 0 else []
    state = row.get("status")
    if row.get("command"):
        state = f"{state} ({row['command']})" if state else row["command"]
    return {
        "id": int(row["session_id"]),
        "user": row.get("login_name"),
        "app": row.get("program_name") or None,
        "client": row.get("host_name"),
        "state": state,
        "wait": wait or None,
        "started": _iso(row.get("login_time")),
        "queryStarted": _iso(row.get("start_time")
                             or row.get("last_request_start_time")),
        "blockedBy": blocked_by,
        "database": row.get("database_name"),
        "query": query,
        "queryHidden": hidden,
    }


def mssql_lock(row: Mapping[str, Any]) -> dict[str, Any]:
    sid = row.get("request_session_id")
    return {
        "id": int(sid) if sid is not None else None,
        "type": row.get("resource_type"),
        "mode": row.get("request_mode"),
        "granted": (row.get("request_status") or "").upper() == "GRANT",
        "relation": row.get("relation"),
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def read_postgres(cur: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    sessions: list[dict[str, Any]] = []
    notes: list[str] = []
    any_hidden = False
    for row in _rows(cur, PG_SESSIONS_SQL):
        s, no_privilege = pg_session(row)
        any_hidden = any_hidden or no_privilege
        sessions.append(s)
    if any_hidden:
        notes.append(NOTE_PG_NO_STATS)
    locks = [pg_lock(r) for r in _rows(cur, PG_LOCKS_SQL)]
    return sessions, locks, notes


def read_mssql(cur: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    perm = _rows(cur, MSSQL_PERMISSION_SQL)
    has_state = bool(perm and perm[0].get("has_state"))
    sessions = [mssql_session(r) for r in _rows(cur, MSSQL_SESSIONS_SQL)]
    if not has_state:
        # sys.dm_tran_locks needs the same permission. Skip it rather than
        # fail the whole view on a permission error.
        notes.append(NOTE_MSSQL_OWN_ONLY)
        return sessions, [], notes
    locks = [mssql_lock(r) for r in _rows(cur, MSSQL_LOCKS_SQL)]
    return sessions, locks, notes


def fetch(target: Any, database: str) -> dict[str, Any]:
    """Read sessions and locks of `database` on `target` with the RO login.

    Raises ValueError for an engine without a viewer, LookupError when the RO
    credential is missing, and the driver's own error when the target is
    unreachable. The caller maps each one to an HTTP status."""
    engine = (getattr(target, "engine", None) or "postgres").lower()
    if engine not in ("postgres", "mssql"):
        raise ValueError(UNSUPPORTED)
    user, password = targets.get_credentials(target.id, "ro")
    fetched_at = datetime.now(timezone.utc).isoformat()

    if engine == "postgres":
        with _connect_pg(target, user, password, database) as conn:
            with conn.cursor() as cur:
                sessions, locks, notes = read_postgres(cur)
            conn.rollback()
    else:
        conn = _connect_mssql(target, user, password, database)
        try:
            cur = conn.cursor()
            try:
                sessions, locks, notes = read_mssql(cur)
            finally:
                cur.close()
            conn.rollback()
        finally:
            conn.close()

    _add_blocking(sessions)
    return {
        "engine": engine,
        "database": database,
        "fetchedAt": fetched_at,
        "sessions": sessions,
        "locks": locks,
        "notes": notes,
    }
