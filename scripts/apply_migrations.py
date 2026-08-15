"""Apply SQL files in migrations/ in lexical order.

Every migration is tracked in a `schema_migrations` ledger (version +
checksum + applied_at), so re-running the script only applies what is
pending instead of re-executing every file. A whole-run advisory lock
prevents two runners from racing, and a checksum mismatch on an
already-applied file stops the run (a committed migration must never be
edited in place).

Usage:
    python scripts/apply_migrations.py            # apply pending
    python scripts/apply_migrations.py --dry-run  # show the plan, change nothing
    python scripts/apply_migrations.py --baseline # record all files as applied
                                                  # WITHOUT running them (adopt the
                                                  # ledger on an existing database)
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dba_slack_bot import db  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Arbitrary fixed key so concurrent invocations serialize on one lock.
_ADVISORY_LOCK_KEY = 728041

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text        PRIMARY KEY,
    filename   text        NOT NULL,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan(entries: list[tuple[str, str]], applied: dict[str, str]) -> list[tuple[str, str]]:
    """Pure planner. `entries` = [(version, checksum)] in apply order,
    `applied` = {version: checksum} already recorded. Returns
    [(action, version)] where action is 'apply', 'skip', or 'dirty'."""
    out: list[tuple[str, str]] = []
    for version, checksum in entries:
        if version not in applied:
            out.append(("apply", version))
        elif applied[version] != checksum:
            out.append(("dirty", version))
        else:
            out.append(("skip", version))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    baseline = "--baseline" in argv

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("No migration files found.")
        return 1

    entries = [(f.name, _checksum(f.read_text())) for f in files]
    by_name = {f.name: f for f in files}

    db.init_pool()
    # One connection for the whole run so the advisory lock (session-scoped,
    # survives commits) is held from first apply to last.
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        conn.commit()
        try:
            cur.execute(_LEDGER_DDL)
            conn.commit()

            cur.execute("SELECT version, checksum FROM schema_migrations")
            recorded = {r["version"]: r["checksum"] for r in cur.fetchall()}

            steps = plan(entries, recorded)
            checksums = dict(entries)

            dirty = [v for action, v in steps if action == "dirty"]
            if dirty:
                print("DIRTY: these applied migrations changed on disk since "
                      "they were recorded — refusing to run:")
                for v in dirty:
                    print(f"  - {v}")
                print("Resolve by reverting the file, or add a new migration "
                      "instead of editing a committed one.")
                return 2

            pending = [v for action, v in steps if action == "apply"]
            if not pending:
                print("Nothing to do — all migrations already applied.")
                return 0

            for version in pending:
                if dry_run:
                    verb = "would record (baseline)" if baseline else "would apply"
                    print(f"{verb}: {version}")
                    continue
                if baseline:
                    cur.execute(
                        "INSERT INTO schema_migrations (version, filename, checksum) "
                        "VALUES (%s, %s, %s)",
                        (version, version, checksums[version]),
                    )
                    conn.commit()
                    print(f"baseline: recorded {version} (not run)")
                    continue
                # Commit the migration and its ledger row together, so the
                # ledger can never claim a migration that did not fully apply.
                print(f"Applying {version}...")
                cur.execute(by_name[version].read_text())
                cur.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum) "
                    "VALUES (%s, %s, %s)",
                    (version, version, checksums[version]),
                )
                conn.commit()
        finally:
            # A failed migration leaves the transaction aborted, so this unlock
            # used to raise InFailedSqlTransaction and MASK the real error — the
            # operator saw a confusing secondary traceback instead of the SQL
            # that failed. Roll back first, and never let a cleanup problem
            # become the reported failure; the lock is session-scoped and clears
            # when the connection closes.
            try:
                conn.rollback()
                cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
                conn.commit()
            except Exception:
                print("warning: could not release the migration advisory lock "
                      "(session-scoped; clears when this connection closes)",
                      file=sys.stderr)

    print("Done." if not dry_run else "Dry run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
