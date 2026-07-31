"""Report (and optionally fix) tables the RO role cannot actually read.

The schema catalog is built from pg_catalog, which every role may read — so
QueryHub happily lists a table, offers it in autocomplete, and then fails the
query with "permission denied for table ...". That gap appears whenever an
object is created by a role the RO grants never covered:
ALTER DEFAULT PRIVILEGES attaches to a CREATING role, so objects made by a
different role inherit nothing — and, most often, because the RO role was
never made a member of `pg_read_all_data`. (Seen on two targets in this fleet:
71 relations the catalog listed and the RO user could not read.)

Read-only by default: prints, per target and database, every relation the RO
role is missing SELECT on. `--fix` re-grants SELECT across all non-system
schemas and installs default privileges for every owning role, which is
idempotent and safe to repeat.

Connects with the operator's own wallet login (~/.pgpass), NOT the bot's
credentials — granting is an operator action.

Usage:
    python3 scripts/audit_ro_readability.py [--target ALIAS] [--database DB] [--fix]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import psycopg  # noqa: E402

from queryhub import schema_catalog, targets  # noqa: E402

log = logging.getLogger("audit-ro")

WALLET_USER = "ahmet_rende"     # resolved from ~/.pgpass; override with --user
RO_ROLE = "queryhub_ro"

_UNREADABLE_SQL = """
SELECT n.nspname AS schema_name, c.relname AS rel,
       pg_get_userbyid(c.relowner) AS owner
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','p','v','m','f')
  AND n.nspname NOT IN ('pg_catalog','information_schema')
  AND n.nspname NOT LIKE 'pg\\_%%'
  AND NOT has_table_privilege(%s, c.oid, 'SELECT')
ORDER BY 1, 2
"""

_SCHEMAS_SQL = """
SELECT nspname FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog','information_schema')
  AND nspname NOT LIKE 'pg\\_%'
ORDER BY 1
"""


def _connect(host: str, dbname: str, user: str):
    return psycopg.connect(
        host=host, port=5432, dbname=dbname, user=user,
        connect_timeout=10, sslmode="require",
        application_name="queryhub-ro-audit",
        options="-c statement_timeout=120000",
    )


def _fix_database(cur) -> None:
    """Close the gap. `pg_read_all_data` (PG 14+) is the real fix — one
    cluster-wide membership that covers every table in every schema, now and
    in the future. The per-schema GRANTs after it are the fallback for a
    server without that role, and they only ever cover what exists today."""
    # AUTOCOMMIT, deliberately. These statements are independent and some are
    # expected to fail (you can only set default privileges for a role you are
    # a member of). Inside one transaction, rolling back a single failure
    # discarded every grant made before it — the first run of this script
    # reported success per statement and committed almost nothing.
    cur.connection.autocommit = True

    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'pg_read_all_data'")
    if cur.fetchone():
        try:
            cur.execute(f"GRANT pg_read_all_data TO {RO_ROLE}")
            log.info("    granted pg_read_all_data")
        except psycopg.Error as e:
            log.warning("    pg_read_all_data grant failed: %s",
                        str(e).strip().splitlines()[0])
    cur.execute(_SCHEMAS_SQL)
    for (s,) in cur.fetchall():
        cur.execute(f'GRANT USAGE ON SCHEMA "{s}" TO {RO_ROLE}')
        cur.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{s}" TO {RO_ROLE}')
        cur.execute(f'GRANT SELECT ON ALL SEQUENCES IN SCHEMA "{s}" TO {RO_ROLE}')
        cur.execute(
            "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
            "WHERE c.relnamespace = %s::regnamespace "
            "  AND c.relkind IN ('r','p','v','m','S','f')", (s,))
        for (owner,) in cur.fetchall():
            for what in ("TABLES", "SEQUENCES"):
                try:
                    cur.execute(
                        f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" '
                        f'IN SCHEMA "{s}" GRANT SELECT ON {what} TO {RO_ROLE}')
                except psycopg.Error as e:
                    # Expected when we aren't a member of the owning role —
                    # skip that role, keep going (autocommit means the grants
                    # already made stay).
                    log.warning("    default privs skipped (%s in %s): %s",
                                owner, s, str(e).strip().splitlines()[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", help="only this target alias")
    ap.add_argument("--database", help="only this database")
    ap.add_argument("--user", default=WALLET_USER, help="wallet login to connect as")
    ap.add_argument("--fix", action="store_true",
                    help="re-grant (default: report only)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    fleet = [t for t in targets.list_enabled()
             if (getattr(t, "engine", None) or "postgres") == "postgres"]
    if args.target:
        fleet = [t for t in fleet if t.alias == args.target]
        if not fleet:
            log.error("no enabled postgres target with alias %r", args.target)
            return 1

    total_missing = 0
    for t in fleet:
        databases = ([args.database] if args.database
                     else schema_catalog.list_snapshot_databases(t.id))
        for dbname in databases:
            try:
                with _connect(t.host, dbname, args.user) as cn:
                    with cn.cursor() as cur:
                        if args.fix:
                            _fix_database(cur)   # runs in autocommit
                        cur.execute(_UNREADABLE_SQL, (RO_ROLE,))
                        miss = cur.fetchall()
            except Exception as e:  # noqa: BLE001 — one bad DB must not stop the sweep
                log.warning("%s/%s: skipped (%s)", t.alias, dbname,
                            type(e).__name__)
                continue
            if miss:
                total_missing += len(miss)
                log.info("%s/%s: %d unreadable", t.alias, dbname, len(miss))
                for schema_name, rel, owner in miss[:20]:
                    log.info("    %s.%s (owner=%s)", schema_name, rel, owner)
                if len(miss) > 20:
                    log.info("    ... and %d more", len(miss) - 20)
            elif args.fix:
                log.info("%s/%s: clean", t.alias, dbname)

    log.info("done: %d relation(s) the RO role still cannot read%s",
             total_missing, "" if args.fix else "  (re-run with --fix to grant)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
