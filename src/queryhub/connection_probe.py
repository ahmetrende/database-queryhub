"""Open one connection to a target, read its version, close it.

Two callers share this probe: the admin "Test" button
(web/routes_admin.py, `_probe`) and the periodic connection health check
(connection_health.py). Both must see the same answer for the same target, so
the code that dials each engine lives here once.

A failed probe is a successful call: "cannot reach it" is the answer, not an
error. The message goes through the same scrubber that the executor uses
before a libpq or ODBC error reaches a user, so a failure cannot echo back the
connection string that the probe got.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from . import config as cfg
from . import errors

log = logging.getLogger(__name__)

REDACTED = "[redacted]"

NO_DRIVER = "The QueryHub host has no driver for this engine."


def probe(engine: str, host: str, port: int, database: str,
          username: str, password: str, *, timeout_sec: int,
          redact: Iterable[str] = (),
          application_name: str = "queryhub-connection-test") -> dict:
    """Open one connection with the given credential, read the server version,
    close. Returns {ok, latencyMs, serverVersion, error}.

    `timeout_sec` bounds the connect, and on PostgreSQL the version read too.
    `redact` names values that must never reach the message, such as the
    password and the login name. The probe replaces them in the raw driver
    text before the scrubber reads it, and again after."""
    return measure(
        lambda: _server_version(engine, host, port, database, username,
                                password, timeout_sec, application_name),
        engine=engine, host=host, port=port, redact=redact)


def measure(dial: Callable[[], Any], *, engine: str, host: str | None = None,
            port: int | None = None, redact: Iterable[str] = ()) -> dict:
    """Run `dial`, time it, and turn a failure into a scrubbed message.

    `dial` returns the server version (or None) and raises when it cannot
    reach the target. The shape is the probe's: {ok, latencyMs,
    serverVersion, error}."""
    secrets = tuple(v for v in redact if v)
    started = time.monotonic()
    try:
        version = dial()
    except ImportError as e:
        # The SQL Server driver is optional and only installed on hosts that
        # serve MSSQL targets. Say that plainly — the scrubbed pyodbc message
        # ("No module named ...") reads like a bug in QueryHub.
        log.warning("connection test: driver unavailable for %s: %s", engine,
                    hide_values(repr(e), secrets))
        return {"ok": False, "latencyMs": None, "serverVersion": None,
                "error": NO_DRIVER}
    except Exception as e:
        log.info("connection test failed for %s:%s (%s): %s",
                 host, port, engine, hide_values(repr(e), secrets))
        return {"ok": False,
                "latencyMs": int((time.monotonic() - started) * 1000),
                "serverVersion": None,
                "error": failure_message(e, host=host, redact=secrets)}
    return {"ok": True, "latencyMs": int((time.monotonic() - started) * 1000),
            "serverVersion": str(version) if version else None, "error": None}


def failure_message(e: BaseException | str, *, host: str | None = None,
                    redact: Iterable[str] = ()) -> str:
    """The text of a failed connection, safe to show and to store."""
    secrets = tuple(v for v in redact if v)
    message = errors.scrub(hide_values(str(e), secrets))
    # scrub() redacts the hostname SHAPES it knows — managed-cloud
    # endpoints, private IPv4 ranges, libpq `host=` fragments — and a
    # target on a private domain matches none of them, so libpq's
    # `connection to server at "..."` preamble survives it. The probe is
    # the one place that knows exactly which host it dialled, so redact it
    # by value rather than hoping a pattern covers the next naming scheme.
    if host:
        message = message.replace(host, "the host")
    # Again after the scrubber: it unwraps a pyodbc tuple, whose repr escapes
    # a quote that the raw value carries, and it can change the case of the
    # first letter.
    return hide_values(message, secrets)


def hide_values(text: str, values: Iterable[str]) -> str:
    """`text` with each value replaced by [redacted], ignoring case.

    It matches a value wherever it appears, also inside a longer word: a
    password can sit next to other characters. It matches a value shorter
    than `_WHOLE_WORD_BELOW` characters (a login such as `sa`) as a whole word
    only, so that the same letters inside `usage` stay readable.

    The longest value goes first. A login name inside the password would
    otherwise replace part of it and leave the rest of the password behind."""
    for value in sorted({v for v in values if v}, key=len, reverse=True):
        pattern = re.escape(value)
        if len(value) < _WHOLE_WORD_BELOW:
            pattern = rf"(?<!\w){pattern}(?!\w)"
        text = re.sub(pattern, REDACTED, text, flags=re.IGNORECASE)
    return text


_WHOLE_WORD_BELOW = 4


def _server_version(engine: str, host: str, port: int, database: str,
                    username: str, password: str, timeout_sec: int,
                    application_name: str) -> str | None:
    """Dial `engine` and return its version. Raises when it cannot connect."""
    if engine == "mssql":
        from . import mssql_exec
        conn = mssql_exec.connect(host, port, database, username, password,
                                  timeout_sec=timeout_sec)
        try:
            cur = conn.cursor()
            cur.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(64))")
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    if engine == "clickhouse":
        from . import clickhouse_exec
        return clickhouse_exec.probe(host, port, database, username,
                                     password, timeout_sec=timeout_sec)
    import psycopg
    with psycopg.connect(
            host=host, port=port, dbname=database, user=username,
            password=password, connect_timeout=timeout_sec,
            **cfg.target_ssl_kwargs(host),
            application_name=application_name,
            options=f"-c statement_timeout={int(timeout_sec) * 1000} "
                    "-c default_transaction_read_only=on") as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            row = cur.fetchone()
            return row[0] if row else None
