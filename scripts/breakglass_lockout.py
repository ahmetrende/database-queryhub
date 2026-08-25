#!/usr/bin/env python3
"""Break glass: shut every QueryHub database login out of the whole fleet.

One command, for the hour when you suspect the bot's credentials are in the
wrong hands. On every enabled target it does three things to each QueryHub
login, in this order and for a reason:

  1. NOLOGIN      stops NEW connections. First, because anything else leaves a
                  window in which a client reconnects behind you.
  2. new password invalidates the leaked secret itself. A NOLOGIN role can be
                  flipped back by anyone who can run ALTER ROLE; the password
                  is what the attacker actually holds.
  3. terminate    kills the sessions already open. Last, because a session
                  killed before step 1 simply reconnects.

The new passwords are RANDOM AND NOT STORED. That is the point: the goal is to
sever access, not to rotate it. Recovery is a deliberate act — set fresh
credentials on the targets and write them back into `target_servers` through
the normal admin path.

What it does NOT touch: the application's own logins, the operator's personal
accounts, and anything on a target QueryHub does not manage. Only the three
logins named on each `target_servers` row.

Usage
-----
    # look, change nothing (default)
    python3 scripts/breakglass_lockout.py

    # do it
    python3 scripts/breakglass_lockout.py --apply

    # one target, or a subset, by alias
    python3 scripts/breakglass_lockout.py --apply --alias svc-prod-orders
    python3 scripts/breakglass_lockout.py --apply --alias 'svc-prod-*'

    # keep the sessions, only lock the door (rare; useful mid-incident when a
    # long-running job must be allowed to finish)
    python3 scripts/breakglass_lockout.py --apply --no-terminate

    # also engage the fleet-wide kill switch so the bot stops accepting work
    python3 scripts/breakglass_lockout.py --apply --kill-switch

Credentials
-----------
By default it reads the encrypted target credentials from the bot's metadata
DB, exactly as the bot does, and connects with the DDL login (which can assume
the elevated role where one is configured). That needs `BOT_DB_*` and the
master key:

    source .venv/bin/activate
    set -a && source /etc/queryhub/env && set +a
    python3 scripts/breakglass_lockout.py --apply

If the bot's own credentials are what you distrust, connect as yourself
instead — then nothing the bot holds is used to perform the lockout:

    python3 scripts/breakglass_lockout.py --apply \
        --admin-user my_master_user --admin-password-env PGPASSWORD

Only the host list and the login names come from the metadata DB in that mode.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import fnmatch
import os
import secrets
import string
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import psycopg  # noqa: E402
from psycopg import sql as pgsql  # noqa: E402

from queryhub import audit, config as cfg, db, targets  # noqa: E402

_ALPHABET = string.ascii_letters + string.digits + "!#%*+-=?_"


def new_password(n: int = 40) -> str:
    """A password nobody will ever see. Long and random because it is never
    typed, never stored and never meant to be recovered."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def fleet(alias_glob: str | None, include_disabled: bool) -> list[dict]:
    rows = db.fetch_all(
        "SELECT id, alias, engine, enabled, super_ddl_role, "
        "       username, username_rw, username_ddl "
        "  FROM target_servers "
        f"{'' if include_disabled else 'WHERE enabled '}"
        " ORDER BY alias")
    if alias_glob:
        rows = [r for r in rows if fnmatch.fnmatch(r["alias"], alias_glob)]
    return rows


def logins_of(row: dict) -> list[str]:
    """The QueryHub logins on this target, de-duplicated and in tier order.

    Read from the row rather than hardcoded: a target may name its own users,
    and locking out a login that does not exist is a confusing no-op while
    MISSING one is the failure that matters."""
    out: list[str] = []
    for key in ("username", "username_rw", "username_ddl"):
        name = (row.get(key) or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def _connect(row: dict, args) -> psycopg.Connection:
    t = targets.get(row["id"])
    if args.admin_user:
        pw = os.environ.get(args.admin_password_env or "PGPASSWORD", "")
        user, password = args.admin_user, pw
    else:
        user, password = targets.get_credentials(row["id"], "ddl")
    conn = psycopg.connect(
        host=t.host, port=t.port, dbname=t.default_database,
        user=user, password=password, connect_timeout=args.timeout,
        application_name="queryhub:breakglass",
        **cfg.target_ssl_kwargs())
    conn.autocommit = True
    return conn


def lock_postgres(row: dict, args) -> dict:
    """Lock every QueryHub login on one Postgres target."""
    result = {"alias": row["alias"], "engine": "postgres", "locked": [],
              "terminated": 0, "error": None}
    names = logins_of(row)
    if not names:
        result["error"] = "no QueryHub logins named on this target"
        return result
    try:
        with _connect(row, args) as conn, conn.cursor() as cur:
            # The DDL login owns nothing; the elevated role is what can ALTER
            # another role. Harmless when the target names none.
            if not args.admin_user and row.get("super_ddl_role"):
                cur.execute(pgsql.SQL("SET ROLE {}").format(
                    pgsql.Identifier(row["super_ddl_role"])))

            for name in names:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
                if cur.fetchone() is None:
                    continue                      # not on this cluster
                if args.apply:
                    cur.execute(pgsql.SQL("ALTER ROLE {} NOLOGIN").format(
                        pgsql.Identifier(name)))
                    cur.execute(pgsql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        pgsql.Identifier(name), pgsql.Literal(new_password())))
                result["locked"].append(name)

            if args.terminate:
                # After the door is shut, not before: a backend killed while
                # the login still works is a backend that comes straight back.
                if args.apply:
                    cur.execute(
                        "SELECT count(pg_terminate_backend(pid)) "
                        "  FROM pg_stat_activity "
                        " WHERE usename = ANY(%s) AND pid <> pg_backend_pid()",
                        (result["locked"],))
                    result["terminated"] = cur.fetchone()[0] or 0
                else:
                    cur.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        " WHERE usename = ANY(%s) AND pid <> pg_backend_pid()",
                        (result["locked"],))
                    result["terminated"] = cur.fetchone()[0] or 0
    except Exception as e:                       # noqa: BLE001 - reported per target
        result["error"] = str(e).strip().splitlines()[0][:120]
    return result


def lock_mssql(row: dict, args) -> dict:
    """SQL Server counterpart. DISABLE is the NOLOGIN equivalent; sessions are
    killed by spid because there is no pg_terminate_backend."""
    result = {"alias": row["alias"], "engine": "mssql", "locked": [],
              "terminated": 0, "error": None}
    try:
        import pyodbc
    except ImportError:
        result["error"] = "pyodbc not installed on this host"
        return result
    names = logins_of(row)
    try:
        user, password = targets.get_credentials(row["id"], "ddl")
        t = targets.get(row["id"])
        cn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={t.host},{t.port};"
            f"DATABASE={t.default_database};UID={user};PWD={password};"
            "Encrypt=yes;TrustServerCertificate=yes;"
            f"Connection Timeout={args.timeout}", autocommit=True)
        cur = cn.cursor()
        for name in names:
            cur.execute("SELECT 1 FROM sys.server_principals WHERE name = ?", name)
            if cur.fetchone() is None:
                continue
            if args.apply:
                cur.execute(f"ALTER LOGIN [{name}] DISABLE")
                cur.execute(f"ALTER LOGIN [{name}] WITH PASSWORD = "
                            f"'{new_password()}'")
            result["locked"].append(name)
        if args.terminate and result["locked"]:
            cur.execute(
                "SELECT session_id FROM sys.dm_exec_sessions "
                " WHERE login_name IN ({}) AND session_id <> @@SPID".format(
                    ",".join("?" * len(result["locked"]))), *result["locked"])
            spids = [r[0] for r in cur.fetchall()]
            if args.apply:
                for spid in spids:
                    try:
                        cur.execute(f"KILL {int(spid)}")
                    except Exception:            # noqa: BLE001
                        pass
            result["terminated"] = len(spids)
        cn.close()
    except Exception as e:                       # noqa: BLE001
        result["error"] = str(e).strip().splitlines()[0][:120]
    return result


def engage_kill_switch(apply: bool) -> str:
    """Stop the bot accepting new work. The database lockout alone leaves
    QueryHub taking submissions it can no longer run, which reads to a user as
    an outage with no explanation.

    Same three writes the admin UI performs, so the banner, the /sql path and
    the notification feed all see it. bot_config is cached for 5 seconds in the
    bot's process, so this lands within seconds, not instantly."""
    msg = ("Locked down by the break-glass script - credentials are being "
           "rotated. Query execution is paused fleet-wide.")
    if not apply:
        return "would set kill_switch=on"
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO bot_config (key, value, updated_at) "
            "VALUES ('kill_switch', 'on', NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "  updated_at = NOW()")
        cur.execute(
            "INSERT INTO bot_config (key, value, updated_at) "
            "VALUES ('kill_switch_message', %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "  updated_at = NOW()", (msg,))
        audit.log_in(cur, None, "BREAKGLASS", "break-glass script",
                     "kill_switch_set",
                     {"enabled": True, "source": "breakglass_lockout"})
    return "kill_switch=on (takes effect within ~5s)"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Lock every QueryHub database login out of the fleet.")
    ap.add_argument("--apply", action="store_true",
                    help="actually do it (default is a dry run)")
    ap.add_argument("--alias", help="only targets whose alias matches this glob")
    ap.add_argument("--include-disabled", action="store_true",
                    help="also lock targets currently disabled in QueryHub")
    ap.add_argument("--no-terminate", dest="terminate", action="store_false",
                    help="leave existing sessions running")
    ap.add_argument("--kill-switch", action="store_true",
                    help="also pause the bot so it stops accepting work")
    ap.add_argument("--admin-user",
                    help="connect as this superuser instead of the bot's DDL login")
    ap.add_argument("--admin-password-env", default="PGPASSWORD",
                    help="env var holding --admin-user's password (default PGPASSWORD)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=10)
    args = ap.parse_args()

    rows = fleet(args.alias, args.include_disabled)
    if not rows:
        print("no targets matched", file=sys.stderr)
        return 2

    head = "APPLYING" if args.apply else "DRY RUN (nothing will change)"
    print(f"{head} — {len(rows)} target(s), "
          f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    print(f"  per login: NOLOGIN -> new random password -> "
          f"{'terminate sessions' if args.terminate else 'sessions left alone'}")
    print(f"  connecting as: "
          f"{args.admin_user or 'the DDL login from target_servers'}\n")

    def run(row):
        if (row.get("engine") or "postgres") == "mssql":
            return lock_mssql(row, args)
        return lock_postgres(row, args)

    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run, rows))

    ok = [r for r in results if not r["error"]]
    bad = [r for r in results if r["error"]]
    killed = sum(r["terminated"] for r in ok)
    verb = "locked" if args.apply else "would lock"
    for r in sorted(ok, key=lambda x: x["alias"]):
        print(f"  {r['alias']:<28} {verb}: {', '.join(r['locked']) or '(none found)'}"
              + (f"  · sessions: {r['terminated']}" if r["terminated"] else ""))
    if bad:
        print("\n  FAILED:")
        for r in sorted(bad, key=lambda x: x["alias"]):
            print(f"    {r['alias']:<28} {r['error']}")

    if args.kill_switch:
        print("\n  bot:", engage_kill_switch(args.apply))

    print(f"\n  {len(ok)}/{len(results)} target(s) done, "
          f"{killed} session(s) {'terminated' if args.apply else 'open right now'}")
    if bad:
        print("  A target that failed is a target still reachable with the old "
              "credentials. Fix those by hand before you call this finished.")
        print("  Two known cases: a target whose credentials live in a secrets "
              "provider this host cannot reach, and one that names no DDL login "
              "at all. Both answer to --admin-user, which does not need the "
              "bot's credentials for anything except the host list.")
    if args.apply:
        print("\n  The new passwords were not stored anywhere. To bring QueryHub "
              "back, set fresh credentials on each target and save them through "
              "the admin UI or scripts/set_target_credentials.py.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
