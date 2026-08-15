"""Re-arm fail-secure bot_config toggles left off past the TTL.

Some toggles are safe only when ON: turning them off removes a guard.
If one is flipped off "just to debug" and forgotten, the bot runs
unprotected indefinitely. This re-enables any such toggle that has been
'off' longer than `security_override_ttl_minutes`, using the updated_at
that the migration-056 trigger maintains.

Fail-secure keys: ast_safety_enabled, pii_masking_enabled, pre_flight_explain.
(kill_switch is excluded — ON is its safe state, so it never auto-reverts.)

Safety / opt-in:
  - Dry-run by default. `--commit` required to change anything.
  - Even with `--commit`, nothing reverts unless bot_config
    `security_override_ttl_enabled` is 'on'.
  - Re-enabling is the fail-secure direction, so this can only tighten,
    never loosen. Each revert is audited and DM'd to active admins.

Usage:
    set -a; source /etc/slackbot/env; set +a
    .venv/bin/python scripts/revert_security_overrides.py            # dry-run
    .venv/bin/python scripts/revert_security_overrides.py --commit   # act (if enabled)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dba_slack_bot import admins, audit, db  # noqa: E402
from dba_slack_bot import config as cfg  # noqa: E402
from dba_slack_bot.config import ENV  # noqa: E402

log = logging.getLogger("security-revert")

FAIL_SECURE_KEYS = ["ast_safety_enabled", "pii_masking_enabled", "pre_flight_explain"]
_OFF_VALUES = ("off", "false", "no", "0")


def find_overdue(ttl_minutes: int) -> list[dict]:
    """Fail-secure toggles that are off and have been off past the TTL."""
    return db.fetch_all(
        "SELECT key, value, updated_at FROM bot_config "
        " WHERE key = ANY(%s) "
        "   AND lower(value) = ANY(%s) "
        "   AND updated_at < NOW() - make_interval(mins => %s) "
        " ORDER BY key",
        (FAIL_SECURE_KEYS, list(_OFF_VALUES), ttl_minutes),
    )


def _ttl_enabled() -> bool:
    return (cfg.get_setting("security_override_ttl_enabled", "off") or "").strip().lower() \
        in {"on", "true", "yes", "1"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Actually re-arm (only acts when security_override_ttl_enabled='on').")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_pool()

    ttl = cfg.get_int("security_override_ttl_minutes", 60)
    overdue = find_overdue(ttl)
    log.info("fail-secure toggles off > %dm: %d", ttl, len(overdue))
    for r in overdue:
        log.info("  %s = %s (since %s)", r["key"], r["value"], r["updated_at"])
    if not overdue:
        return 0

    if not args.commit:
        log.info("dry-run (no --commit) — nothing re-armed.")
        return 0
    if not _ttl_enabled():
        log.info("security_override_ttl_enabled is off — --commit is a no-op. "
                 "Set it 'on' in bot_config to let the TTL re-arm toggles.")
        return 0

    from slack_sdk.web import WebClient
    from dba_slack_bot.slack_app import notifications
    client = WebClient(token=ENV.slack_bot_token)
    admin_ids = [a["slack_user_id"] for a in admins.list_active()]
    reverted = 0
    for r in overdue:
        with db.transaction() as cur:
            # The trigger also logs a generic 'security_config_changed';
            # this records the auto-revert intent + how long it was off.
            cur.execute(
                "UPDATE bot_config SET value = 'on' "
                " WHERE key = %s AND lower(value) = ANY(%s)",
                (r["key"], list(_OFF_VALUES)),
            )
            if cur.rowcount == 0:
                continue  # raced — someone re-enabled it already
            audit.log_in(cur, None, None, "security ttl", "security_override_auto_reverted",
                         {"key": r["key"], "ttl_minutes": ttl, "was_off_since": str(r["updated_at"])})
        reverted += 1
        for aid in admin_ids:
            try:
                notifications.dm_requester(
                    client, aid,
                    f":lock: Auto-re-armed `{r['key']}` -> *on* — it had been off longer "
                    f"than the {ttl}m security TTL.")
            except Exception:
                log.exception("revert notify failed for admin %s", aid)
    log.info("re-armed %d toggle(s)", reverted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
