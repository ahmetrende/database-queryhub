"""Purge expired CSV results — local files AND Slack uploads.

Designed to run from the daily DBA Job-Runner. Idempotent; safe to run as
often as you like. Files are kept for `bot_config.results_ttl_hours` (default
48) past their last modification. Override via SQL:
    UPDATE bot_config SET value = '24' WHERE key = 'results_ttl_hours';

Local cleanup:
    /var/lib/slackbot/results/*.{csv,zip,xlsx}  →  removed if mtime older
    than TTL (all three artifact types the executor writes).

Slack cleanup:
    requests.slack_file_id           →  files.delete via Slack API for any
                                        request whose completed_at is older
                                        than TTL. Row's slack_file_id and
                                        csv_file_path are then NULL'd so we
                                        don't retry forever.

Audit row is left intact — only the artifact (CSV file + Slack upload) is
removed. The DBA can still see what was queried, by whom, and when.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dba_slack_bot import config as cfg  # noqa: E402
from dba_slack_bot import db  # noqa: E402


def _slack_client():
    """Slack client, or None when this deployment has no Slack.

    slack_sdk is an OPTIONAL extra: the vanilla profile never installs it. This
    module used to import it at top level, so on a vanilla install the ONLY
    retention job for query results died with ImportError before deleting
    anything — result CSVs (which hold real query output) accumulated on disk
    forever. Local deletion must work everywhere; only the Slack-side file
    deletion is conditional."""
    if not cfg.ENV.slack_enabled:
        return None
    try:
        from slack_sdk import WebClient
    except ImportError:
        log_ = logging.getLogger(__name__)
        log_.warning("slack_sdk not installed — skipping Slack file deletion; "
                     "local files are still cleaned")
        return None
    return WebClient(token=cfg.ENV.slack_bot_token)

RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "/var/lib/slackbot/results"))
IMPORT_DIR = Path(os.environ.get("IMPORT_DIR", "/var/lib/slackbot/imports"))

log = logging.getLogger("slackbot-cleanup")


def cleanup_local(ttl_hours: int) -> tuple[int, int]:
    cutoff = time.time() - ttl_hours * 3600
    deleted = kept = 0
    if not RESULTS_DIR.exists():
        log.info("local: %s does not exist; nothing to do", RESULTS_DIR)
        return 0, 0
    # The executor writes .csv (single result), .zip (2+ CSVs bundled) and
    # .xlsx (Excel format) — reap all three, not just .csv, or zip/xlsx
    # artifacts accumulate forever.
    for f in RESULTS_DIR.iterdir():
        if not f.is_file() or f.suffix.lower() not in (".csv", ".zip", ".xlsx"):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
            else:
                kept += 1
        except FileNotFoundError:
            pass  # raced with another cleanup or the bot
        except OSError as e:
            log.warning("local: could not remove %s: %s", f, e)
    log.info("local: deleted=%d kept=%d (cutoff=%dh)", deleted, kept, ttl_hours)
    return deleted, kept


def cleanup_slack(ttl_hours: int) -> tuple[int, int]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    rows = db.fetch_all(
        "SELECT id, slack_file_id FROM requests "
        "WHERE slack_file_id IS NOT NULL AND completed_at IS NOT NULL "
        "  AND completed_at < %s "
        "ORDER BY completed_at",
        (cutoff,),
    )
    if not rows:
        log.info("slack: nothing past TTL")
        return 0, 0

    client = _slack_client()
    if client is None:
        log.info("slack: no Slack in this deployment — skipping %d file(s)",
                 len(rows))
        return 0, 0
    deleted = errors = 0
    for r in rows:
        file_id = r["slack_file_id"]
        try:
            client.files_delete(file=file_id)
            deleted += 1
        except Exception as e:      # slack_sdk.SlackApiError (lazily imported)
            err = (getattr(e, "response", None) or {}).get("error", "unknown")
            # Already gone is fine — clear our reference too.
            if err in ("file_not_found", "file_deleted"):
                deleted += 1
            else:
                log.warning(
                    "slack: files.delete failed for request %d (file %s): %s",
                    r["id"], file_id, err,
                )
                errors += 1
                continue
        # Clear references so we never try this row again.
        db.execute(
            "UPDATE requests SET slack_file_id = NULL, csv_file_path = NULL "
            "WHERE id = %s",
            (r["id"],),
        )
    log.info("slack: deleted=%d errors=%d (cutoff=%dh)", deleted, errors, ttl_hours)
    return deleted, errors


def cleanup_imports(ttl_hours: int) -> tuple[int, int]:
    """Purge uploaded import CSVs (local + Slack) past their own TTL.
    Imports keep a tighter window than query results."""
    deleted = kept = 0
    cutoff_t = time.time() - ttl_hours * 3600
    if IMPORT_DIR.exists():
        for f in IMPORT_DIR.glob("*.csv"):
            try:
                if f.stat().st_mtime < cutoff_t:
                    f.unlink(); deleted += 1
                else:
                    kept += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("imports: could not remove %s: %s", f, e)
    # Clear DB references + delete any Slack upload for old import rows.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    rows = db.fetch_all(
        "SELECT id, slack_file_id FROM csv_imports "
        "WHERE created_at < %s AND csv_file_path IS NOT NULL",
        (cutoff,),
    )
    client = _slack_client() if rows else None
    for r in rows:
        if r.get("slack_file_id") and client is not None:
            try:
                client.files_delete(file=r["slack_file_id"])
            except Exception as e:  # slack_sdk.SlackApiError (lazily imported)
                err = (getattr(e, "response", None) or {}).get("error", "")
                if err not in ("file_not_found", "file_deleted"):
                    log.warning("imports: files.delete failed for %d: %s", r["id"], err)
        db.execute("UPDATE csv_imports SET csv_file_path=NULL, slack_file_id=NULL "
                   "WHERE id=%s", (r["id"],))
    log.info("imports: local_deleted=%d kept=%d db_cleared=%d (cutoff=%dh)",
             deleted, kept, len(rows), ttl_hours)
    return deleted, kept


def cleanup_web_sessions(days: int) -> None:
    """Purge server-synced named workspaces untouched for `days` (mig 064).
    Local (browser) sessions are never stored server-side, so this only
    affects the dest='server' rows the user chose to sync. Saved queries in
    the tree are kept (they have no last-touched semantics here)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    db.execute("DELETE FROM web_saved_sessions WHERE updated_at < %s", (cutoff,))
    log.info("web-sessions: purged server workspaces untouched > %dd", days)


def cleanup_auth_sessions(grace_days: int) -> None:
    """Delete LOGIN sessions that can no longer authenticate anything.

    Distinct from cleanup_web_sessions above, which purges saved workspaces —
    `web_sessions` is the auth table, one row per sign-in, and nothing ever
    removed from it. Rows accumulate forever, each holding a refresh-token hash
    and the principal it belonged to. A revoked or long-expired session is
    inert, so keeping it is only a retention liability. The grace period keeps
    recent rows around so a revocation stays auditable for a while."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
    db.execute(
        "DELETE FROM web_sessions "
        " WHERE (expires_at < %s) OR (revoked_at IS NOT NULL AND revoked_at < %s)",
        (cutoff, cutoff))
    log.info("auth-sessions: purged expired/revoked logins older than %dd",
             grace_days)


def cleanup_auth_event_outbox(days: int) -> None:
    """Delete processed authorization-change outbox rows past `days`.

    The outbox is drained by the DM poller, but processed rows were never
    removed, so the table only grew — and in a deployment with no Slack the
    poller does not run at all, so even unprocessed rows piled up forever. The
    web process now runs the poller too (see web/app.py), and this trims what it
    has already handled."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    db.execute("DELETE FROM auth_event_outbox WHERE processed_at < %s", (cutoff,))
    log.info("auth-outbox: purged processed rows older than %dd", days)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.init_pool()
    ttl = cfg.get_int(
        "results_ttl_hours",
        int(os.environ.get("RESULTS_TTL_HOURS", "48")),
    )
    cleanup_local(ttl)
    cleanup_slack(ttl)
    # Imports have their own (tighter) TTL.
    import_ttl = cfg.get_int("import_csv_ttl_hours", 24)
    cleanup_imports(import_ttl)
    # Server-synced web workspaces: 30-day retention.
    cleanup_web_sessions(cfg.get_int("web_session_retention_days", 30))
    # Auth sessions + the authorization-change outbox: tables that only grew.
    cleanup_auth_sessions(cfg.get_int("auth_session_retention_days", 7))
    cleanup_auth_event_outbox(cfg.get_int("auth_outbox_retention_days", 14))
    return 0


if __name__ == "__main__":
    sys.exit(main())
