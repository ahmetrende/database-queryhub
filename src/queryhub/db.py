"""Connection pool + helpers for the bot's metadata DB."""
from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from .config import ENV

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None

# The control DB can be unreachable at start (a host that has just booted, a
# failover). The process must not stay broken until someone restarts it.
# A failed connect leaves no pool behind. The next query tries again, at most
# once per RETRY_COOLDOWN_SEC and for at most RETRY_WAIT_SEC. Inside the
# cooldown a query fails at once, so a busy outage does not pile every worker
# thread onto a connect that cannot work.
RETRY_COOLDOWN_SEC = 5.0
RETRY_WAIT_SEC = 5.0
_FIRST_WAIT_SEC = 30.0
_connect_failed_at: float | None = None  # time.monotonic() of the last failure
_init_lock = threading.RLock()


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


def init_pool(min_size: int | None = None, max_size: int | None = None,
              wait_timeout: float = _FIRST_WAIT_SEC) -> None:
    """Create the metadata connection pool (idempotent).

    When the pool does not come up within `wait_timeout` seconds, this raises
    and keeps no pool. psycopg_pool closes the pool itself after such a
    timeout. This function used to keep that closed pool. Every later query
    then failed with "the pool is already closed", and `pool_ready()` said
    True, until the process restarted.

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
    global _pool, _connect_failed_at
    with _init_lock:
        if _pool is not None:
            return
        if min_size is None:
            min_size = _env_int("QH_DB_POOL_MIN", 1, low=1, high=64)
        if max_size is None:
            max_size = _env_int("QH_DB_POOL_MAX", 10, low=1, high=200)
        if max_size < min_size:
            log.warning("QH_DB_POOL_MAX (%s) is below QH_DB_POOL_MIN (%s); "
                        "using %s for both", max_size, min_size, min_size)
            max_size = min_size
        log.info("metadata pool: min_size=%s max_size=%s", min_size, max_size)
        pool = ConnectionPool(
            conninfo=_conninfo(),
            min_size=min_size,
            max_size=max_size,
            kwargs={
                "row_factory": dict_row,
                "autocommit": False,
                "password": ENV.bot_db_password,
                **ENV.bot_db_ssl_kwargs(),
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
        try:
            pool.wait(timeout=wait_timeout)
        except Exception:
            pool.close()  # stops the pool's worker threads; safe to repeat
            _connect_failed_at = time.monotonic()
            raise
        _pool = pool
        _connect_failed_at = None


def _ensure_pool() -> ConnectionPool:
    """The pool, connecting again when an earlier attempt failed.

    The first attempt waits as long as init_pool() does. A retry after a
    failure waits RETRY_WAIT_SEC, and none starts inside RETRY_COOLDOWN_SEC of
    the last failure.
    """
    pool = _pool
    if pool is not None:
        return pool
    with _init_lock:
        if _pool is None:
            failed_at = _connect_failed_at
            if failed_at is None:
                init_pool()
            else:
                left = RETRY_COOLDOWN_SEC - (time.monotonic() - failed_at)
                if left > 0:
                    raise PoolTimeout("control database unreachable; the next "
                                      f"connect attempt is in {left:.0f} s")
                log.info("control database: connecting again")
                init_pool(wait_timeout=RETRY_WAIT_SEC)
                log.warning("control database: connected again")
        assert _pool is not None
        return _pool


def close_pool() -> None:
    global _pool, _connect_failed_at
    # The lock that _ensure_pool() takes, so that no thread opens a new pool
    # while this one closes.
    with _init_lock:
        _connect_failed_at = None
        if _pool is not None:
            _pool.close()
            _pool = None


# Close the pool while the interpreter can still join its worker threads. A
# script that ends with the pool open left it to the pool's __del__, which runs
# later, during finalization. On Python 3.14 that join raises
# PythonFinalizationError, and every scheduled job printed a traceback.
atexit.register(close_pool)


def pool_ready() -> bool:
    """True once the pool is open. For startup work that should be skipped,
    not retried, when the control DB was unreachable at boot."""
    return _pool is not None


@contextmanager
def connection() -> Iterator[Any]:
    with _ensure_pool().connection() as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[Any]:
    """Multi-statement atomic transaction. Yields a cursor; commits on
    successful exit, rolls back on exception. Use when multiple writes
    must succeed or fail together — most importantly when pairing a
    state-changing UPDATE with its audit_log INSERT, so we never end up
    with one without the other."""
    with _ensure_pool().connection() as conn:
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
