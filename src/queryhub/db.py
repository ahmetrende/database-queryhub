"""Connection pool + helpers for the bot's metadata DB."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import ENV

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    """Read a positive int from the environment, clamped and fail-soft.

    A typo in a pool size must not stop the process from starting — it logs and
    uses the default instead.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default
    if not (low <= val <= high):
        log.warning("%s=%s is outside %s..%s; using %s", name, val, low, high,
                    default)
        return default
    return val


def _conninfo() -> str:
    # Password is intentionally NOT embedded here — it's passed via
    # `kwargs` to ConnectionPool below. A conninfo string can leak through
    # tracebacks (e.g. when the pool fails to initialize and the exception
    # repr includes it); keeping the password out of the string limits the
    # leak surface to deliberate logging only.
    return (
        f"host={ENV.bot_db_host} port={ENV.bot_db_port} "
        f"dbname={ENV.bot_db_name} user={ENV.bot_db_user} "
        f"application_name=dba-slack-bot"
    )


def init_pool(min_size: int | None = None, max_size: int | None = None) -> None:
    """Create the metadata connection pool (idempotent).

    Size comes from the environment rather than a constant. It was max_size=5,
    with all ~11 call sites passing nothing, no environment variable, and no
    mention in .env.example or the docs — while essentially every web route is a
    synchronous `def`, so Starlette runs them on the anyio worker threadpool
    (40 threads by default) and the executor adds four workers of its own. Under
    load those threads queue on five connections, and the number they are
    queueing on appears nowhere an operator can see or change.

    Env-only on purpose, not bot_config: reading bot_config needs the pool this
    function creates. Documented in .env.example and docs/CONFIGURATION.md.
    """
    global _pool
    if _pool is None:
        if min_size is None:
            min_size = _env_int("QH_DB_POOL_MIN", 1, low=1, high=64)
        if max_size is None:
            max_size = _env_int("QH_DB_POOL_MAX", 10, low=1, high=200)
        if max_size < min_size:
            log.warning("QH_DB_POOL_MAX (%s) is below QH_DB_POOL_MIN (%s); "
                        "using %s for both", max_size, min_size, min_size)
            max_size = min_size
        log.info("metadata pool: min_size=%s max_size=%s", min_size, max_size)
        _pool = ConnectionPool(
            conninfo=_conninfo(),
            min_size=min_size,
            max_size=max_size,
            kwargs={
                "row_factory": dict_row,
                "autocommit": False,
                "password": ENV.bot_db_password,
                # Cap any individual statement at 10s and any
                # idle-in-transaction at 30s so a stuck metadata-DB query
                # can't block the connection pool indefinitely. All bot
                # metadata queries are simple — 10s is generous.
                "options": (
                    "-c statement_timeout=10000 "
                    "-c idle_in_transaction_session_timeout=30000"
                ),
            },
        )
        _pool.wait()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[Any]:
    if _pool is None:
        init_pool()
    assert _pool is not None
    with _pool.connection() as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[Any]:
    """Multi-statement atomic transaction. Yields a cursor; commits on
    successful exit, rolls back on exception. Use when multiple writes
    must succeed or fail together — most importantly when pairing a
    state-changing UPDATE with its audit_log INSERT, so we never end up
    with one without the other."""
    if _pool is None:
        init_pool()
    assert _pool is not None
    with _pool.connection() as conn:
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def execute(sql: str, params: tuple | None = None) -> None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()


def fetch_one(sql: str, params: tuple | None = None) -> dict | None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: tuple | None = None) -> list[dict]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def insert_returning(sql: str, params: tuple) -> dict:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        conn.commit()
        if row is None:
            raise RuntimeError("INSERT...RETURNING returned no row")
        return row
