"""Refresh the schema catalog for every enabled target (Postgres + SQL Server).

For each target: enumerate its databases (via the default database),
snapshot each one's system catalog into the bot DB with the RO credential.
The read is engine-dispatched in schema_catalog (pg_catalog for Postgres,
sys.* for SQL Server); the stored shape is identical either way.
Unreachable targets / databases the RO role can't connect to are skipped
and reported — one bad target must not sink the fleet run.

A target that stops refreshing is REPORTED, not just logged. Browse, search
and the /sql autocomplete keep serving the last good snapshot with nothing on
screen saying it is old, so a target can sit stale for days -- an expired
credential, a host that moved, a path that closed -- and the first symptom is
somebody asking why a table they just created is missing. After
`schema_refresh_alert_after` consecutive failures (default 3, i.e. ~3 hours)
the admins get one DM, and one more when it recovers.

Run hourly from the host scheduler:
    python3 scripts/refresh_schema_catalog.py [--target ALIAS] [--database DB]
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import admins, crypto, db, schema_catalog  # noqa: E402
from queryhub import config as cfg  # noqa: E402
from queryhub import targets as targets_mod  # noqa: E402

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
    notices: list[tuple[str, str]] = []   # (alias, message)
    alert_after = cfg.get_int("schema_refresh_alert_after", 3)
    for target in fleet:
        summary = refresh_target(target, only_database=args.database)
        for database, outcome in summary.items():
            level = logging.INFO
            if outcome.startswith(("failed", "unreachable", "skipped")):
                level = logging.WARNING
                failures += 1
            log.log(level, "%s/%s: %s", target.alias, database, outcome)

        # Health is recorded for a WHOLE-target run only. A --target or
        # --database run is an operator poking one thing by hand; letting it
        # reset or trip the streak would make the hourly signal depend on who
        # ran what in between.
        if args.target or args.database:
            continue
        ok, error = schema_catalog.summary_is_ok(summary)
        try:
            verdict = schema_catalog.record_refresh(
                target.id, ok, error, alert_after)
        except Exception:  # noqa: BLE001 — health must never sink the refresh
            log.exception("refresh health bookkeeping failed for %s", target.alias)
            continue
        if verdict["alert"]:
            stale = verdict["stale_hours"]
            since = (f"{stale:.0f}h" if stale is not None else "ever")
            notices.append((target.alias,
                            f":warning: Schema catalog for `{target.alias}` has "
                            f"not refreshed in {since} "
                            f"({verdict['failures']} runs in a row). Browse, "
                            f"search and `/sql` autocomplete are serving an old "
                            f"snapshot for it.\nLast error: {error}"))
        elif verdict["recovered"]:
            notices.append((target.alias,
                            f":white_check_mark: Schema catalog for "
                            f"`{target.alias}` refreshed again."))

    _announce(notices)
    log.info("done: %d targets, %d skipped/failed entries, %d notice(s)",
             len(fleet), failures, len(notices))
    return 0


def _announce(notices: list[tuple[str, str]]) -> None:
    """DM the admins, once per notice. Best-effort: a Slack outage must not
    cost the refresh — the health row is already written either way, so the
    alert simply goes out on the next run."""
    if not notices:
        return
    try:
        from slack_sdk.web import WebClient
        from queryhub.db import ENV
        from queryhub.slack_app import notifications
        client = WebClient(token=ENV.slack_bot_token)
        recipients = sorted({a["slack_user_id"] for a in admins.list_active()
                             if (a.get("slack_user_id") or "").startswith("U")})
    except Exception:  # noqa: BLE001
        log.exception("could not build the notifier; notices not sent")
        return
    for alias, text in notices:
        for uid in recipients:
            try:
                notifications.dm_requester(client, uid, text)
            except Exception:  # noqa: BLE001
                log.exception("catalog notice for %s to %s failed", alias, uid)


if __name__ == "__main__":
    sys.exit(main())
