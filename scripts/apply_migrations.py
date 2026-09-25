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

Once the metadata roles are split (scripts/split_metadata_roles.py), the
runtime login cannot run DDL, so this runs as the migrator instead:
BOT_DB_MIGRATOR_USER and BOT_DB_MIGRATOR_PASSWORD, plus BOT_DB_OWNER_ROLE. It
connects as the migrator, runs `SET ROLE <owner>` so every object it creates is
owned by the owner, and after the last file re-applies the runtime's grants
(metadata_roles.runtime_grants): a new table gets DML, a new view SELECT only.
Keep those variables out of the services' environment. Without them it runs as
BOT_DB_USER, as before.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import db  # noqa: E402
from queryhub import metadata_roles  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Arbitrary fixed key so concurrent invocations serialize on one lock.
_LOCK_TIMEOUT = "3s"
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


def _migrator() -> tuple[str, str, str] | None:
    """(user, password, owner) when the roles are split, None when not."""
    user = (os.environ.get("BOT_DB_MIGRATOR_USER") or "").strip()
    if not user:
        return None
    owner = (os.environ.get("BOT_DB_OWNER_ROLE") or "").strip()
    if not owner:
        raise SystemExit("BOT_DB_MIGRATOR_USER is set but BOT_DB_OWNER_ROLE is not: "
                         "the migrator must know which role owns the objects")
    return user, os.environ.get("BOT_DB_MIGRATOR_PASSWORD", ""), owner


@contextlib.contextmanager
def _connection(migrator):
    """The runtime pool as before, or a direct connection as the migrator.

    The migrator's connection has no statement_timeout: the pool's 10 s cap is
    for the services' metadata reads, and it cancelled any migration that had
    to build an index on a large table. lock_timeout still applies."""
    if migrator is None:
        db.init_pool()
        with db.connection() as conn:
            yield conn
        return
    import psycopg
    from psycopg.rows import dict_row

    from queryhub.config import ENV
    user, password, owner = migrator
    with psycopg.connect(host=ENV.bot_db_host, port=ENV.bot_db_port,
                         dbname=ENV.bot_db_name, user=user, password=password,
                         application_name="queryhub:migrations",
                         row_factory=dict_row, **ENV.bot_db_ssl_kwargs()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('role', %s, false)", (owner,))
        conn.commit()
        yield conn


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

    migrator = _migrator()
    # One connection for the whole run so the advisory lock (session-scoped,
    # survives commits) is held from first apply to last.
    with _connection(migrator) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        # Never queue for a lock on a live table. A migration that adds a
        # column takes ACCESS EXCLUSIVE, and if anything is holding a
        # conflicting lock the ALTER waits — with every reader that arrives
        # afterwards queued behind it. On the busiest table that turns a
        # metadata-only change into an outage. Fail fast instead and let the
        # operator re-run: a migration that has to wait is a migration that
        # should be attempted at a quieter moment.
        cur.execute(f"SET lock_timeout = '{_LOCK_TIMEOUT}'")
        conn.commit()
        try:
            # Create the ledger only when it is missing. CREATE ... IF NOT
            # EXISTS still needs CREATE on the schema, which a split runtime
            # login does not have, so it failed even a --dry-run.
            cur.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL AS ok")
            if not cur.fetchone()["ok"]:
                if dry_run:
                    print("would create the schema_migrations ledger")
                else:
                    cur.execute(_LEDGER_DDL)
                conn.commit()

            recorded: dict[str, str] = {}
            try:
                cur.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL AS ok")
                if cur.fetchone()["ok"]:
                    cur.execute("SELECT version, checksum FROM schema_migrations")
                    recorded = {r["version"]: r["checksum"] for r in cur.fetchall()}
            except Exception as e:
                if getattr(e, "sqlstate", None) == "42501":  # insufficient_privilege
                    print("The metadata roles are split and this login cannot read "
                          "the ledger. Run with BOT_DB_MIGRATOR_USER, "
                          "BOT_DB_MIGRATOR_PASSWORD and BOT_DB_OWNER_ROLE set.",
                          file=sys.stderr)
                    return 2
                raise

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
            if migrator is not None and not dry_run and not baseline:
                from queryhub.config import ENV
                grants = metadata_roles.runtime_grants(cur, ENV.bot_db_user, migrator[2])
                for stmt in grants:
                    cur.execute(stmt)
                conn.commit()
                print(f"runtime grants: {len(grants)} change(s) for {ENV.bot_db_user}")
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
