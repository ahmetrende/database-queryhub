"""Refresh the schema catalog for every enabled target (Postgres + SQL Server).

For each target: enumerate its databases (via the default database),
snapshot each one's system catalog into the bot DB with the RO credential.
The read is engine-dispatched in schema_catalog (pg_catalog for Postgres,
sys.* for SQL Server); the stored shape is identical either way.
Unreachable targets / databases the RO role can't connect to are skipped
and reported — one bad target must not sink the fleet run.

Run hourly from the host scheduler:
    python3 scripts/refresh_schema_catalog.py [--target ALIAS] [--database DB]
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dba_slack_bot import crypto, db, schema_catalog  # noqa: E402
from dba_slack_bot import targets as targets_mod  # noqa: E402

log = logging.getLogger("refresh_schema_catalog")


def _ro_password(target_id: int) -> str | None:
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT password_encrypted FROM target_servers WHERE id = %s",
            (target_id,),
        )
        row = cur.fetchone()
    if not row or not row["password_encrypted"]:
        return None
    password = crypto.decrypt(row["password_encrypted"])
    return None if password == "PASSWORD_NOT_SET" else password


def refresh_target(target, only_database: str | None = None) -> dict:
    """Snapshot every database on one target. Returns a per-DB summary."""
    summary: dict[str, str] = {}
    password = _ro_password(target.id)
    if password is None:
        return {"*": "skipped: no RO credential"}
    try:
        databases = schema_catalog.list_target_databases(target, password)
    except Exception as e:  # noqa: BLE001 — keep the fleet run alive
        return {"*": f"unreachable: {type(e).__name__}"}
    if only_database:
        databases = [d for d in databases if d == only_database]
    for database in databases:
        try:
            t0 = time.monotonic()
            n_tables, n_cols = schema_catalog.snapshot_database(
                target, password, database)
            summary[database] = (
                f"{n_tables} tables / {n_cols} columns "
                f"({time.monotonic() - t0:.1f}s)")
        except Exception as e:  # noqa: BLE001
            summary[database] = f"failed: {type(e).__name__}: {e}"
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="only this target alias")
    parser.add_argument("--database", help="only this database")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    fleet = targets_mod.list_enabled()
    if args.target:
        fleet = [t for t in fleet if t.alias == args.target]
        if not fleet:
            log.error("no enabled target with alias %r", args.target)
            return 1

    failures = 0
    for target in fleet:
        summary = refresh_target(target, only_database=args.database)
        for database, outcome in summary.items():
            level = logging.INFO
            if outcome.startswith(("failed", "unreachable", "skipped")):
                level = logging.WARNING
                failures += 1
            log.log(level, "%s/%s: %s", target.alias, database, outcome)

    log.info("done: %d targets, %d skipped/failed entries", len(fleet), failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
