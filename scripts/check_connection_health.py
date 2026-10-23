"""Check that QueryHub can connect to each target, and store the answer.

One run:

1. Reads the four `connection_health_*` settings from bot_config. When
   `connection_health_enabled` is off, it exits 0 at once and checks nothing.
2. Takes an advisory lock. When another run holds it, it exits 0.
3. Deletes the stored result of each target that the check no longer covers:
   disabled, marked deleted, or an excluded engine.
4. Checks every enabled target that is not deleted, whose engine is not
   excluded, and whose last check is at least `connection_health_interval_sec`
   old. At most 8 checks run at once. Each one gets
   `connection_health_timeout_sec`.
5. Writes one row per checked target to `connection_health`.

GET /admin/connections shows the row as `health`. The rules, the probe and the
cost of each engine are in src/queryhub/connection_health.py and in
docs/OPERATIONS.md, "Connection health".

Run it from the host scheduler, every minute. The interval setting decides how
often a run checks each target, so a changed interval needs no edit here:
    python3 scripts/check_connection_health.py

Print what a run would do. This opens no connection to a target and writes
nothing:
    python3 scripts/check_connection_health.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import connection_health as health  # noqa: E402
from queryhub import db  # noqa: E402

log = logging.getLogger("check_connection_health")


def print_plan(p: health.Plan, s: health.Settings) -> None:
    excluded = ", ".join(sorted(s.exclude_engines)) or "none"
    print(f"interval {s.interval_sec} s, timeout {s.timeout_sec} s, "
          f"excluded engines: {excluded}")
    for t in p.check:
        last = t.get("last_checked_at")
        print(f"check   {t['alias']} ({t['engine']}): "
              f"{'never checked' if last is None else f'last checked {last.isoformat()}'}")
    for t, why in p.skip:
        print(f"skip    {t['alias']} ({t['engine']}): {why}")
    for t in p.delete:
        print(f"delete  {t['alias']}: its stored result, because the check "
              "no longer covers it")
    print(f"{len(p.check)} to check, {len(p.skip)} to skip, {len(p.delete)} "
          "stored result(s) to delete. Nothing was changed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the connection to each target and store the result.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan: open no connection to a target "
                             "and write nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # The lock's connection, the writes, and a metadata read for each check
    # that runs at the same time.
    db.init_pool(min_size=1, max_size=health.MAX_PARALLEL + 3)
    s = health.settings()
    if not s.enabled:
        log.info("connection_health_enabled is off: nothing to check")
        return 0
    for name in s.unknown_engines:
        log.warning("connection_health_exclude_engines names %r, which is no "
                    "engine: it excludes nothing", name)

    if args.dry_run:
        print_plan(health.plan(health.Store().load(), s, health.utcnow()), s)
        return 0

    with health.run_lock() as held:
        if not held:
            log.info("another run holds the lock: nothing to check")
            return 0
        summary = health.run_once(health.Store(), s)
    log.info("done: %d checked (%d failing), %d skipped, %d old result(s) deleted",
             summary.checked, summary.failing, summary.skipped, summary.deleted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
