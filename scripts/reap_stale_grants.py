"""Stale-grant reaper — soft-revoke idle per-user target grants.

A `user_target_grants` row is stale when the grant is older than
`grant_idle_revoke_days` AND the user has made no request to that target in
the same window. Stale grants are soft-revoked (revoked_at = NOW()); every
read of user_target_grants filters revoked_at IS NULL, so access stops but
the row + config stay for audit / re-grant.

Safety:
  - Dry-run by default. `--commit` is required to change anything.
  - Even with `--commit`, nothing is revoked unless bot_config
    `grant_reaper_enabled` is 'on'. So the daily job can run `--commit`
    harmlessly until an operator flips the switch after reviewing dry-runs.

Usage:
    set -a; source /etc/slackbot/env; set +a
    .venv/bin/python scripts/reap_stale_grants.py            # dry-run report
    .venv/bin/python scripts/reap_stale_grants.py --commit   # act (if enabled)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dba_slack_bot import audit, db  # noqa: E402
from dba_slack_bot import config as cfg  # noqa: E402
from dba_slack_bot.config import ENV  # noqa: E402

log = logging.getLogger("grant-reaper")


def find_stale(days: int) -> list[dict]:
    """Active user grants that are older than `days` and whose user has not
    queried the target within `days`."""
    return db.fetch_all(
        "SELECT g.slack_user_id, g.target_server_id, g.mode, ts.alias, g.granted_at "
        "  FROM user_target_grants g "
        "  JOIN target_servers ts ON ts.id = g.target_server_id "
        " WHERE g.revoked_at IS NULL "
        "   AND g.granted_at < NOW() - make_interval(days => %s) "
        "   AND NOT EXISTS ( "
        "         SELECT 1 FROM requests r "
        "          WHERE r.requester_slack_id = g.slack_user_id "
        "            AND r.target_server_id   = g.target_server_id "
        "            AND r.created_at > NOW() - make_interval(days => %s)) "
        " ORDER BY g.slack_user_id, ts.alias",
        (days, days),
    )


def _reaper_enabled() -> bool:
    return (cfg.get_setting("grant_reaper_enabled", "off") or "").strip().lower() \
        in {"on", "true", "yes", "1"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Actually revoke (only acts when grant_reaper_enabled='on').")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_pool()

    days = cfg.get_int("grant_idle_revoke_days", 30)
    stale = find_stale(days)
    log.info("stale user grants (idle > %d days): %d", days, len(stale))
    for g in stale:
        log.info("  %s  ->  %s  (%s, granted %s)",
                 g["slack_user_id"], g["alias"], g["mode"], g["granted_at"].date())
    if not stale:
        return 0

    if not args.commit:
        log.info("dry-run (no --commit) — nothing revoked.")
        return 0
    if not _reaper_enabled():
        log.info("grant_reaper_enabled is off — --commit is a no-op. "
                 "Set it 'on' in bot_config once the dry-run looks right.")
        return 0

    # Live revoke. DM the user per revoked grant so they know.
    from slack_sdk.web import WebClient
    from dba_slack_bot.slack_app import notifications
    client = WebClient(token=ENV.slack_bot_token)
    revoked = 0
    for g in stale:
        with db.transaction() as cur:
            # The reaper DMs the user itself (below) — suppress the
            # auth-event outbox so the trigger doesn't double-DM.
            cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
            cur.execute(
                "UPDATE user_target_grants SET revoked_at = NOW() "
                " WHERE slack_user_id = %s AND target_server_id = %s "
                "   AND revoked_at IS NULL",
                (g["slack_user_id"], g["target_server_id"]),
            )
            if cur.rowcount == 0:
                continue  # raced / already revoked
            audit.log_in(cur, None, "SYSTEM", "grant reaper", "grant_revoked_idle",
                         {"user": g["slack_user_id"], "target": g["alias"],
                          "target_id": g["target_server_id"], "mode": g["mode"],
                          "idle_days": days})
        revoked += 1
        try:
            notifications.dm_requester(
                client, g["slack_user_id"],
                f":lock: Your *{g['mode'].upper()}* access to `{g['alias']}` was disabled "
                f"after {days} days without a query. Re-request via `/sql` if you still need it.")
        except Exception:
            log.exception("revoke notify failed for %s", g["slack_user_id"])
    log.info("revoked %d stale grant(s)", revoked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
