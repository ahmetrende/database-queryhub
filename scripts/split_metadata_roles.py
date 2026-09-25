#!/usr/bin/env python3
"""Move the metadata database off a single owning login (SEC-ROLES).

Before: the runtime login (BOT_DB_USER) owns the database and every object in
it, so it can UPDATE and DELETE `audit_log`. After: a NOLOGIN owner role owns
everything, the runtime keeps DML on tables and only SELECT/INSERT on
`audit_log`, and migrations run as a separate migrator login. The service
configuration does not change: same login, same password. See
docs/OPERATIONS.md "Splitting the metadata database roles" for the runbook,
and src/queryhub/metadata_roles.py for the policy.

    # read-only: print the plan, lock nothing
    python scripts/split_metadata_roles.py --owner queryhub_owner

    # run it inside a transaction, check it, then roll back (takes the locks)
    python scripts/split_metadata_roles.py --owner queryhub_owner --rehearse

    # run it for real
    python scripts/split_metadata_roles.py --owner queryhub_owner --apply

    # put everything back under the runtime login
    python scripts/split_metadata_roles.py --owner queryhub_owner --rollback --apply

It connects as the administrative login given by --admin-user (password in
the environment variable --password-env names), never as the runtime: the
runtime is the role losing privileges, and it cannot hand them over itself.
That login must be able to SET ROLE to the owner and to the current owner.
Roles are not created here; the runbook creates them, because a login's
password is the operator's to choose and never passes through this script.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from queryhub import config as cfg  # noqa: E402
from queryhub import metadata_roles as mr  # noqa: E402

# A table another session is using makes ALTER ... OWNER wait, and every query
# that arrives afterwards queues behind it. Give up quickly and try again at a
# quieter moment instead.
_LOCK_TIMEOUT = "3s"


def _connect(args) -> psycopg.Connection:
    return psycopg.connect(
        host=cfg.ENV.bot_db_host, port=cfg.ENV.bot_db_port,
        dbname=cfg.ENV.bot_db_name, user=args.admin_user,
        password=os.environ.get(args.password_env, ""),
        application_name="queryhub:split-metadata-roles",
        row_factory=dict_row, **cfg.ENV.bot_db_ssl_kwargs())


def _verify(cur, roles: mr.Roles, rollback: bool) -> list[str]:
    """What must hold afterwards, checked inside the same transaction."""
    problems = []
    if rollback:
        left = mr.still_owned_by(cur, roles.owner)
        if left:
            problems.append(f"{left} object(s) are still owned by {roles.owner}")
        return problems
    left = mr.still_owned_by(cur, roles.current_owner)
    if left:
        problems.append(f"{left} object(s) are still owned by {roles.current_owner}")
    cur.execute(
        "SELECT has_table_privilege(%(r)s, 'public.audit_log', 'INSERT') AS ins, "
        "       has_table_privilege(%(r)s, 'public.audit_log', 'UPDATE') AS upd, "
        "       has_table_privilege(%(r)s, 'public.audit_log', 'DELETE') AS del, "
        "       has_table_privilege(%(r)s, 'public.audit_log', 'TRUNCATE') AS trn, "
        "       has_table_privilege(%(r)s, 'public.requests', 'UPDATE') AS req, "
        "       has_database_privilege(%(r)s, current_database(), 'TEMPORARY') AS tmp, "
        "       has_schema_privilege(%(r)s, 'public', 'CREATE') AS ddl",
        {"r": roles.runtime})
    v = cur.fetchone()
    if not v["ins"]:
        problems.append("the runtime cannot INSERT into audit_log")
    if v["upd"] or v["del"] or v["trn"]:
        problems.append("the runtime can still change or remove audit_log rows")
    if not v["req"]:
        problems.append("the runtime lost UPDATE on requests")
    if not v["tmp"]:
        problems.append("the runtime has no TEMPORARY (the mirror trigger needs it)")
    if v["ddl"]:
        problems.append("the runtime can still CREATE in schema public")
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm') "
        "   AND (has_table_privilege(%(r)s, c.oid, 'UPDATE') "
        "        OR has_table_privilege(%(r)s, c.oid, 'DELETE') "
        "        OR has_table_privilege(%(r)s, c.oid, 'INSERT'))", {"r": roles.runtime})
    writable = [r["relname"] for r in cur.fetchall()]
    if writable:
        problems.append(f"the runtime can write through views: {', '.join(writable)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--owner", required=True, help="the NOLOGIN owner role")
    ap.add_argument("--runtime", default=cfg.ENV.bot_db_user,
                    help="the service login (default: BOT_DB_USER)")
    ap.add_argument("--current-owner", default=None,
                    help="who owns the objects now (default: the runtime)")
    ap.add_argument("--admin-user", default=os.environ.get("PGUSER", ""),
                    help="administrative login to run as (default: PGUSER)")
    ap.add_argument("--password-env", default="PGPASSWORD",
                    help="environment variable holding its password")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--rehearse", action="store_true",
                      help="run and check inside a transaction, then roll back")
    mode.add_argument("--apply", action="store_true", help="run and commit")
    ap.add_argument("--rollback", action="store_true",
                    help="plan the reverse: everything back to the runtime")
    args = ap.parse_args(argv)
    if not args.admin_user:
        ap.error("--admin-user (or PGUSER) is required: the runtime cannot "
                 "give its own privileges away")
    if args.admin_user == args.runtime:
        ap.error("run this as an administrative login, not as the runtime")
    roles = mr.Roles(runtime=args.runtime, owner=args.owner,
                     current_owner=args.current_owner or args.runtime)

    with _connect(args) as conn, conn.cursor() as cur:
        if not args.rollback:
            problems = mr.check_split_preconditions(cur, roles)
            if problems:
                print("Cannot split:", *(f"  - {p}" for p in problems), sep="\n")
                return 2
        stmts = (mr.plan_rollback(cur, roles) if args.rollback
                 else mr.plan_split(cur, roles))
        executing = args.apply or args.rehearse
        head = ("APPLYING" if args.apply else "REHEARSAL (rolled back at the end)"
                if args.rehearse else "PLAN (read-only; nothing runs)")
        print(f"-- {head}: {'rollback' if args.rollback else 'split'} for "
              f"runtime={roles.runtime} owner={roles.owner}")
        if executing:
            cur.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
        for s in stmts:
            print(s.as_string(conn) + ";")
            if executing:
                cur.execute(s)
        if not args.rollback:
            if executing:
                grants = mr.runtime_grants(cur, roles.runtime, roles.owner)
            else:
                grants = None
            if grants is None:
                print("-- then the runtime's grants, computed after the transfer: "
                      "DML on tables, SELECT on views, SELECT and INSERT on "
                      "audit_log, nothing on schema_migrations, USAGE on "
                      "sequences, CONNECT and TEMPORARY on the database.")
            else:
                for s in grants:
                    print(s.as_string(conn) + ";")
                    cur.execute(s)
        if not executing:
            conn.rollback()
            print("-- nothing was changed")
            return 0
        problems = _verify(cur, roles, args.rollback)
        if problems:
            conn.rollback()
            print("Checks failed, rolled back:", *(f"  - {p}" for p in problems), sep="\n")
            return 1
        if args.rehearse:
            conn.rollback()
            print("-- checks passed; rolled back as asked")
            return 0
        conn.commit()
        print("-- checks passed; committed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
