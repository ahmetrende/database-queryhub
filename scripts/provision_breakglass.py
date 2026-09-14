"""Provision the break-glass elevation role on Postgres targets.

QueryHub's DDL elevation model: the bot's own DDL credential stays weak;
when a DDL needs table ownership, an admin supplies the break-glass
role's password interactively (memory-only, never stored). This script
automates the Postgres side of that model:

  1. CREATE ROLE dba_breakglass (LOGIN, NOSUPERUSER/NOCREATEDB/
     NOCREATEROLE, no standing password requirement here — password comes
     from $BREAKGLASS_PASSWORD only when the role must be created).
  2. Discover, across EVERY database on the instance, the distinct roles
     that own user objects (tables / partitions / matviews / views /
     sequences / foreign tables, schemas, functions).
  3. GRANT <owner_role> TO dba_breakglass for each — membership is what
     lets the break-glass role run owner-gated DDL. Grants are
     cluster-wide, so one pass per instance covers all its databases.
  4. NEVER auto-grant an owner that is (a member of) rds_superuser or the
     master user — that would silently escalate break-glass to master.
     Those owners are reported for a manual decision (usually: reassign
     the objects to the app owner role instead).

Idempotent and re-runnable: object ownership drifts as app users create
new tables, so re-run periodically (or before big migrations) to close
coverage gaps. Dry-run by default; --commit applies and writes a bot-DB
audit row per instance.

Connections to targets use libpq defaults (the operator's ~/.pgpass
wallet) — deliberately NOT the bot's credentials: the bot must never
hold the power this script grants.

Usage:
    python3 scripts/provision_breakglass.py [--target ALIAS] [--commit]
    BREAKGLASS_PASSWORD=... python3 ... --commit   # only if role missing
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import psycopg  # noqa: E402
from psycopg import sql as pgsql  # noqa: E402

from dba_slack_bot import db  # noqa: E402
from dba_slack_bot import targets as targets_mod  # noqa: E402

log = logging.getLogger("provision-breakglass")

ROLE = "dba_breakglass"
# The admin login used for provisioning (password resolved from ~/.pgpass).
# Set BREAKGLASS_WALLET_USER; defaults to the RDS master convention.
WALLET_USER = os.environ.get("BREAKGLASS_WALLET_USER", "postgres")

_DATABASES_SQL = """
SELECT datname FROM pg_database
WHERE datallowconn AND NOT datistemplate AND datname <> 'rdsadmin'
ORDER BY datname
"""

# Distinct owner roles of user objects in the CURRENT database, split into
# grantable vs flagged (member of rds_superuser, or the session/master
# user). pg_has_role on the break-glass side tells us what is already
# covered so re-runs only add the delta.
_OWNERS_SQL = """
WITH owner_oids AS (
    SELECT c.relowner AS oid
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r','p','m','v','S','f')
      AND n.nspname NOT IN ('pg_catalog','information_schema')
      AND n.nspname NOT LIKE 'pg\\_toast%%' AND n.nspname NOT LIKE 'pg\\_temp%%'
    UNION
    SELECT n.nspowner FROM pg_namespace n
    WHERE n.nspname NOT IN ('pg_catalog','information_schema','public')
      AND n.nspname NOT LIKE 'pg\\_%%'
    UNION
    SELECT p.proowner
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname NOT IN ('pg_catalog','information_schema')
      AND n.nspname NOT LIKE 'pg\\_%%'
)
SELECT r.rolname,
       (EXISTS (SELECT 1 FROM pg_roles bg WHERE bg.rolname = %(role)s)
        AND pg_has_role(%(role)s, r.oid, 'MEMBER')) AS covered,
       (r.rolname = current_user
        OR pg_has_role(r.oid, 'rds_superuser', 'MEMBER')) AS flagged
FROM (SELECT DISTINCT oid FROM owner_oids) o
JOIN pg_roles r ON r.oid = o.oid
WHERE r.rolname NOT LIKE 'rds\\_%%' AND r.rolname <> 'rdsadmin'
  AND r.rolname <> %(role)s
ORDER BY r.rolname
"""


def _connect(host: str, dbname: str):
    # No password argument: libpq falls back to ~/.pgpass (wallet).
    return psycopg.connect(
        host=host, port=5432, dbname=dbname, user=WALLET_USER,
        connect_timeout=8, sslmode="require",
        application_name="queryhub-breakglass-provision",
        options="-c statement_timeout=30000",
    )


def provision_target(target, commit: bool) -> dict:
    """Inspect one instance; apply role + membership grants when --commit.
    Returns a per-instance report dict."""
    report = {"alias": target.alias, "role_exists": None, "role_created": False,
              "grants_planned": [], "grants_applied": [], "flagged": [],
              "already_covered": [], "errors": []}
    try:
        with _connect(target.host, target.default_database) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
                report["role_exists"] = cur.fetchone() is not None
                cur.execute(_DATABASES_SQL)
                databases = [r[0] for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001 — keep the fleet run alive
        report["errors"].append(f"unreachable: {type(e).__name__}")
        return report

    owners: dict[str, dict] = {}
    for database in databases:
        try:
            with _connect(target.host, database) as conn, conn.cursor() as cur:
                cur.execute(_OWNERS_SQL, {"role": ROLE})
                for rolname, covered, flagged in cur.fetchall():
                    seen = owners.setdefault(
                        rolname, {"covered": covered, "flagged": flagged, "dbs": []})
                    seen["dbs"].append(database)
                    seen["flagged"] = seen["flagged"] or flagged
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"{database}: {type(e).__name__}")

    for rolname, info in sorted(owners.items()):
        if info["flagged"]:
            report["flagged"].append(rolname)
        elif info["covered"]:
            report["already_covered"].append(rolname)
        else:
            report["grants_planned"].append(rolname)

    if not commit:
        return report

    try:
        with _connect(target.host, target.default_database) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                if not report["role_exists"]:
                    password = os.environ.get("BREAKGLASS_PASSWORD")
                    if not password:
                        report["errors"].append(
                            "role missing and $BREAKGLASS_PASSWORD not set — "
                            "role not created")
                        return report
                    # INHERIT on purpose: membership must confer the owner
                    # roles' powers passively, so one elevated connection
                    # can run a whole multi-owner migration without a
                    # SET ROLE dance per table.
                    cur.execute(pgsql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT"
                    ).format(pgsql.Identifier(ROLE), pgsql.Literal(password)))
                    report["role_created"] = True
                for rolname in report["grants_planned"]:
                    cur.execute(pgsql.SQL("GRANT {} TO {}").format(
                        pgsql.Identifier(rolname), pgsql.Identifier(ROLE)))
                    report["grants_applied"].append(rolname)
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"apply failed: {type(e).__name__}: {e}")
        return report

    db.execute(
        "INSERT INTO audit_log (actor_slack_id, actor_name, action, details) "
        "VALUES ('operator', 'breakglass provisioner', "
        "        'breakglass_provisioned', %s::jsonb)",
        (json.dumps({
            "target_id": target.id, "alias": target.alias,
            "role_created": report["role_created"],
            "grants": report["grants_applied"],
            "flagged_for_review": report["flagged"],
        }),),
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="only this target alias")
    parser.add_argument("--commit", action="store_true",
                        help="apply role + grants (default: dry-run report)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    fleet = targets_mod.list_enabled()
    if args.target:
        fleet = [t for t in fleet if t.alias == args.target]
        if not fleet:
            log.error("no enabled target with alias %r", args.target)
            return 1

    mode = "COMMIT" if args.commit else "DRY-RUN"
    problems = 0
    for target in fleet:
        rep = provision_target(target, commit=args.commit)
        verb = "applied" if args.commit else "planned"
        log.info(
            "[%s] %s: role_exists=%s%s | grants %s: %s | covered: %d | "
            "FLAGGED (manual): %s | errors: %s",
            mode, rep["alias"], rep["role_exists"],
            " (created)" if rep["role_created"] else "",
            verb,
            rep["grants_applied" if args.commit else "grants_planned"] or "-",
            len(rep["already_covered"]),
            rep["flagged"] or "-",
            rep["errors"] or "-",
        )
        if rep["errors"]:
            problems += 1
    log.info("done: %d target(s), %d with errors", len(fleet), problems)
    return 0


if __name__ == "__main__":
    sys.exit(main())
