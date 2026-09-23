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

ClickHouse targets are NOT read by the hourly run. A ClickHouse Cloud service
can sleep when idle and bills compute when woken, and every query resets its
idle timer: an hourly read would keep the 60-minute services awake for good.
They are read by a separate mode, run every minute:
    python3 scripts/refresh_schema_catalog.py --clickhouse-when-fresh
which reads a service at most once a day, and only when the inventory's state
for it says `running` in a snapshot a few minutes old. The inventory refreshes
that state once an hour, a minute or two past the hour; polling for the fresh
snapshot, rather than running at a fixed minute, is what keeps this aligned
with it. Nothing records which services idle, so every one is treated as one
that does.
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import admins, crypto, db, engines, schema_catalog  # noqa: E402
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
    # An engine whose identity is an assumed role has no credential to be
    # missing. Without this the Athena target would report "skipped: no RO
    # credential" forever -- a true sentence about the wrong engine.
    if password is None and engines.spec(target.engine).requires_credentials:
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


# How old the inventory's ClickHouse snapshot may be and still count as the
# service's state now. The shortest idle timeout on the fleet is 15 minutes, so
# a `running` older than a few minutes may already be `idle`, and reading it
# then would wake it.
_CH_FRESH_MINUTES = 3
# The zone the inventory writes its naive timestamps in.
_INVENTORY_TZ = "Europe/Istanbul"
# At most one read a day per ClickHouse service, attempted or not: a service
# that failed is not retried every hour, because retrying is what wakes it.
_CH_EVERY_HOURS = 23


def _clickhouse_states() -> dict[str, str] | None:
    """{endpoint: state} from the inventory when its ClickHouse snapshot is
    fresh, else None. Read-only, one query."""
    import psycopg
    env = cfg.ENV
    with psycopg.connect(host=env.bot_db_host, port=env.bot_db_port,
                         dbname="inventory", user=env.bot_db_user,
                         password=env.bot_db_password, connect_timeout=10,
                         application_name="queryhub:clickhouse-catalog",
                         options="-c default_transaction_read_only=on") as conn, \
            conn.cursor() as cur:
        # v_server, not `servers`: the bot's login may read the view only.
        # The view does not filter soft-deleted rows, so this does.
        #
        # `updated_at` is a timestamp WITHOUT time zone holding Istanbul local
        # time (measured: 16:01 while now() said 13:07 UTC). Compared with
        # now() as it stands it reads three hours in the future, i.e. always
        # fresh, which is the one wrong answer that wakes services. So it is
        # compared in Istanbul time, and bounded ABOVE too: if that assumption
        # ever stops holding, the snapshot reads as not fresh and nothing is
        # read, rather than everything, every minute.
        cur.execute(
            "SELECT endpoint, db_instance_status, "
            "       max(updated_at) OVER () BETWEEN "
            "         (now() AT TIME ZONE %s) - make_interval(mins => %s) "
            "         AND (now() AT TIME ZONE %s) + interval '1 minute' "
            "  FROM v_server WHERE engine = 'clickhouse' AND NOT is_deleted",
            (_INVENTORY_TZ, _CH_FRESH_MINUTES, _INVENTORY_TZ))
        rows = cur.fetchall()
    if not rows or not rows[0][2]:
        return None
    return {endpoint: (state or "").lower() for endpoint, state, _fresh in rows}


def _attempted_within(target_id: int, hours: int) -> bool:
    row = db.fetch_one(
        "SELECT 1 AS x FROM schema_refresh_health WHERE target_server_id = %s "
        "   AND last_attempt_at > now() - make_interval(hours => %s)",
        (target_id, hours))
    return row is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="only this target alias")
    parser.add_argument("--database", help="only this database")
    parser.add_argument("--clickhouse-when-fresh", action="store_true",
                        help="read the ClickHouse targets that are due, if the "
                             "inventory's state for them is fresh")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    fleet = targets_mod.list_enabled()
    if args.target:
        # An operator naming a target reads it whatever its engine: that is a
        # deliberate wake, not a schedule.
        fleet = [t for t in fleet if t.alias == args.target]
        if not fleet:
            log.error("no enabled target with alias %r", args.target)
            return 1
    elif args.clickhouse_when_fresh:
        clickhouse = [t for t in fleet if t.engine == "clickhouse"]
        if not clickhouse:
            return 0
        states = _clickhouse_states()
        if states is None:
            log.debug("clickhouse: inventory snapshot not fresh; nothing due")
            return 0
        fleet = [t for t in clickhouse
                 if states.get(t.host) == "running"
                 and not _attempted_within(t.id, _CH_EVERY_HOURS)]
        if not fleet:
            return 0
    else:
        fleet = [t for t in fleet if t.engine != "clickhouse"]

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
