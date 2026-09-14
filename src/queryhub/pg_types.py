"""Type loaders for target connections.

Postgres timestamps have two values Python's `datetime` cannot hold:
`infinity` and `-infinity`. psycopg raises `DataError: timestamp too large
(after year 10K)` on either, and it raises while READING the row — so the
failure is not a bad cell, it is the whole result.

That is not an exotic case. `pg_roles.rolvaliduntil` is `infinity` for every
role created without `VALID UNTIL` (three of them on the first server this was
measured on), which made `SELECT * FROM pg_roles` — the query a DBA runs to
answer "who can log in here" — fail outright with a message about the year
10000.

The loaders below return the literal text for the two infinite values and defer
to psycopg for every real timestamp. Text is the honest answer: a result goes
out as CSV or XLSX, so `infinity` reaches the reader as the word Postgres
itself uses, instead of being mapped onto `9999-12-31` (a date that means
something different) or losing the row to an exception.
"""
from __future__ import annotations

import logging

from psycopg.types.datetime import (
    DateLoader,
    TimestampLoader,
    TimestamptzLoader,
)

log = logging.getLogger(__name__)

_INFINITE = (b"infinity", b"-infinity")


class _InfSafeMixin:
    """Return the raw text for `infinity` / `-infinity`, else load normally."""

    def load(self, data):
        if bytes(data) in _INFINITE:
            return bytes(data).decode()
        return super().load(data)


class InfSafeDateLoader(_InfSafeMixin, DateLoader):
    pass


class InfSafeTimestampLoader(_InfSafeMixin, TimestampLoader):
    pass


class InfSafeTimestamptzLoader(_InfSafeMixin, TimestamptzLoader):
    pass


_LOADERS = (
    ("date", InfSafeDateLoader),
    ("timestamp", InfSafeTimestampLoader),
    ("timestamptz", InfSafeTimestamptzLoader),
)


def register_infinity_safe_loaders(conn) -> None:
    """Install the loaders on ONE connection.

    Per connection, not on the global `psycopg.adapters`: the same process also
    talks to the bot's own metadata DB, and its readers expect `datetime`
    objects. Target results are written to a file as text either way, so this is
    exactly the boundary the change belongs on.

    Never raises. A missing loader class in some future psycopg would cost the
    infinity handling, not the query.
    """
    try:
        for name, loader in _LOADERS:
            conn.adapters.register_loader(name, loader)
    except Exception:
        log.warning("could not register infinity-safe loaders", exc_info=True)
