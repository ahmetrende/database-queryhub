"""Auto-expire stale pending requests.

A request stuck in 'pending' (awaiting admin action) past
`pending_expiry_hours` is cancelled so it stops cluttering the queue and
stops standing as an open intent. The requester is DM'd so they can
re-submit.

Mirrors the grant reaper's safety model:
  - Dry-run by default. `--commit` is required to change anything.
  - Even with `--commit`, nothing is cancelled unless bot_config
    `pending_expiry_enabled` is 'on'. So a daily job can run `--commit`
    harmlessly until an operator flips the switch after reviewing
    dry-runs.

Expired requests become 'cancelled' (an existing terminal state every
render/metrics path handles) with decision_reason 'expired ...'; the
forensic record is the dedicated audit_log action 'request_expired'.

Usage:
    set -a; source /etc/slackbot/env; set +a
    .venv/bin/python scripts/expire_stale_pending.py            # dry-run report
    .venv/bin/python scripts/expire_stale_pending.py --commit   # act (if enabled)
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

log = logging.getLogger("pending-expiry")


def find_stale(hours: int) -> list[dict]:
    """Pending requests older than `hours`."""
    return db.fetch_all(
        "SELECT r.id, r.requester_slack_id, r.target_server_id, ts.alias, r.created_at "
        "  FROM requests r "
        "  JOIN target_servers ts ON ts.id = r.target_server_id "
        " WHERE r.status = 'pending' "
        "   AND r.created_at < NOW() - make_interval(hours => %s) "
        " ORDER BY r.created_at",
        (hours,),
    )


def _expiry_enabled() -> bool:
    return (cfg.get_setting("pending_expiry_enabled", "off") or "").strip().lower() \
        in {"on", "true", "yes", "1"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Actually expire (only acts when pending_expiry_enabled='on').")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_pool()

    hours = cfg.get_int("pending_expiry_hours", 24)
    stale = find_stale(hours)
    log.info("stale pending requests (idle > %dh): %d", hours, len(stale))
    for r in stale:
        log.info("  #%s  ->  %s  (created %s)", r["id"], r["alias"], r["created_at"])
    if not stale:
        return 0

    if not args.commit:
        log.info("dry-run (no --commit) — nothing expired.")
        return 0
    if not _expiry_enabled():
        log.info("pending_expiry_enabled is off — --commit is a no-op. "
                 "Set it 'on' in bot_config once the dry-run looks right.")
        return 0

    from slack_sdk.web import WebClient
    from dba_slack_bot.slack_app import notifications
    client = WebClient(token=ENV.slack_bot_token)
    reason = f"expired: no admin action within {hours}h"
    expired = 0
    for r in stale:
        with db.transaction() as cur:
            cur.execute(
                "UPDATE requests SET status = 'cancelled', "
                "  decision_reason = %s, decided_at = NOW() "
                " WHERE id = %s AND status = 'pending'",
                (reason, r["id"]),
            )
            if cur.rowcount == 0:
                continue  # raced — admin acted between scan and now
            audit.log_in(cur, r["id"], None, "pending expiry", "request_expired",
                         {"target": r["alias"], "target_id": r["target_server_id"],
                          "idle_hours": hours})
        expired += 1
        try:
            notifications.dm_requester(
                client, r["requester_slack_id"],
                f":hourglass: Your request *#{r['id']}* to `{r['alias']}` expired after "
                f"{hours}h with no admin action. Re-submit via `/sql` if you still need it.")
        except Exception:
            log.exception("expiry notify failed for #%s", r["id"])
    log.info("expired %d request(s)", expired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
