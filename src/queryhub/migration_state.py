"""Migration files that the metadata database does not have yet.

scripts/apply_migrations.py applies the files in `migrations/` and records each
one in the `schema_migrations` ledger. This module only reads. It compares the
files that ship with this code with the versions in the ledger. Both services
log the difference at startup, and the web app's /readyz answers 503 while the
difference is not empty.

A false "pending" verdict makes a healthy install unready. So each doubt gives
None ("unknown") and a debug line, never a list. These are the doubts:

- There is no migrations directory. A wheel install ships none.
- There is no ledger table. The database is older than the ledger.
- This login cannot read the ledger. After the metadata role split, the
  runtime login has no access to it (metadata_roles.py).
- The ledger holds none of the shipped versions. It belongs to another program.
- There is no connection pool, or the database gives an error.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

LEDGER = "public.schema_migrations"
# Each QueryHub migrations directory holds this file. A directory without it
# belongs to another program.
FIRST_FILE = "001_initial_schema.sql"
# The command that applies the pending files. Run it from the install directory.
APPLY_COMMAND = "python scripts/apply_migrations.py"


def migration_files(directory: Path) -> list[Path]:
    """The migration files in `directory`, in apply order.

    scripts/apply_migrations.py uses this function too. Thus the runner and
    this check always find the same files."""
    return sorted(directory.glob("*.sql"))


def version_of(path: Path) -> str:
    """The ledger version of a migration file: its file name."""
    return path.name


def find_directory() -> Path | None:
    """The migrations directory of this install, or None.

    The search has two places, in this order. The first is the directory next
    to the package in a source checkout. The second is the working directory,
    which is where the container image keeps the files (/app/migrations)."""
    candidates = [Path(__file__).resolve().parents[2] / "migrations"]
    try:
        candidates.append(Path.cwd() / "migrations")
    except OSError:  # the working directory does not exist any more
        pass
    for candidate in candidates:
        if (candidate / FIRST_FILE).is_file():
            return candidate
    return None


def shipped_versions(directory: Path | None = None) -> list[str] | None:
    """The versions of the migration files that ship with this code, in
    apply order. None when there is no migrations directory."""
    try:
        directory = directory or find_directory()
        if directory is None:
            log.debug("migration check skipped: no migrations directory")
            return None
        return [version_of(f) for f in migration_files(directory)]
    except Exception:
        log.debug("migration check skipped: cannot list the migrations "
                  "directory", exc_info=True)
        return None


def ledger_readable() -> bool | None:
    """Whether this login can read the ledger. None when there is no ledger.

    One catalog read tells "no table" (NULL) from "no privilege" (false).
    Neither answer runs a statement that fails, so the pooled connection
    never gets an aborted transaction."""
    row = db.fetch_one(
        "SELECT has_table_privilege(to_regclass(%s), 'SELECT') AS readable",
        (LEDGER,))
    return row["readable"] if row else None


def applied_versions() -> set[str] | None:
    """The versions in the ledger. None when this process cannot read it."""
    if not db.pool_ready():
        # No retry: db.fetch_one would open a pool that failed at boot, and
        # that waits 30 seconds before it gives up.
        log.debug("migration check skipped: no connection pool")
        return None
    try:
        readable = ledger_readable()
        if readable is None:
            log.debug("migration check skipped: no %s table", LEDGER)
            return None
        if not readable:
            log.debug("migration check skipped: this login cannot read %s", LEDGER)
            return None
        rows = db.fetch_all(f"SELECT version FROM {LEDGER}")
    except Exception:
        log.debug("migration check skipped: the ledger read failed", exc_info=True)
        return None
    return {r["version"] for r in rows}


def pending(shipped: Sequence[str] | None) -> list[str] | None:
    """The shipped versions that the ledger does not hold, in apply order.

    An empty list means that no migration is pending. None means unknown.
    This function never raises."""
    if shipped is None:
        return None
    applied = applied_versions()
    if applied is None:
        return None
    if applied and applied.isdisjoint(shipped):
        log.debug("migration check skipped: the ledger holds none of the "
                  "shipped versions")
        return None
    return [v for v in shipped if v not in applied]


def warn_if_pending() -> None:
    """Log one CRITICAL line when migrations are pending. For the startup of
    each service. It never stops the start."""
    try:
        names = pending(shipped_versions())
        if names:
            more = f" and {len(names) - 5} more" if len(names) > 5 else ""
            log.critical("%d migrations pending: %s%s. Apply them with: %s",
                         len(names), ", ".join(names[:5]), more, APPLY_COMMAND)
    except Exception:  # a log line must never decide whether a service starts
        log.debug("migration check skipped", exc_info=True)


class PendingCheck:
    """pending() for a readiness probe, with each verdict kept for a time.

    The constructor reads the file list once, at process start. That list
    belongs to the code that runs. A `git pull` after the start adds files
    that this process does not use, and they must not make it unready."""

    def __init__(self, ttl_seconds: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._shipped = shipped_versions()
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._verdict: list[str] | None = None

    def get(self) -> list[str] | None:
        """The pending versions, [] or None, as pending() gives them."""
        with self._lock:
            now = self._clock()
            if self._checked_at is None or now - self._checked_at >= self._ttl:
                self._verdict = pending(self._shipped)
                self._checked_at = now
            return self._verdict
