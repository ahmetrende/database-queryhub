"""Entry point: starts the Slack Bolt app in Socket Mode."""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import auth_events, db, executor, lifecycle
from .config import ENV
from .slack_app import handlers, notifications


def _setup_logging() -> None:
    # Shared with the web process so both emit the same shape. LOG_FORMAT=json
    # switches to one JSON object per line for a log pipeline; see
    # logging_setup for why that is an env var and not a bot_config key.
    from .logging_setup import configure
    configure(level=ENV.log_level)


def main() -> int:
    _setup_logging()
    log = logging.getLogger("dba_slack_bot")
    # Build stamp: record the exact commit this process runs out of,
    # so an operator reading the logs knows which build is live without
    # guessing from the deploy time.
    try:
        from .web import build_info
        _b = build_info.build()
        log.info("Starting QueryHub (build %s sha %s)",
                 _b.get("version", "?"), _b.get("sha", "?"))
    except Exception:
        log.info("Starting QueryHub")

    # This entrypoint IS the Slack bot. In the vanilla (no-Slack) profile
    # there is nothing for it to do — the web process runs on its own — so
    # fail fast with a clear message instead of crashing deep in Bolt.
    if not ENV.slack_enabled:
        log.error("SLACK_BOT_TOKEN / SLACK_APP_TOKEN are not set — the Slack "
                  "bot cannot start. Run the web process instead (vanilla "
                  "profile), or set the tokens and install '.[slack]'.")
        return 2

    db.init_pool()

    # Sweep lease-expired 'executing' rows (orphaned from a dead process — a
    # query can't survive its connection dying) to failed. Lease-gated so a
    # recent 'executing' row still running in the live web process is left
    # alone; the scheduler loop repeats this sweep so orphans are cleaned up
    # without needing a restart.
    orphaned = executor.reconcile_orphaned_executing()
    if orphaned:
        log.info("Reconciled %d orphaned 'executing' request(s) to failed", orphaned)

    # Bolt auto-enables OAuth + a file-based InstallationStore (and then
    # IGNORES our bot token, breaking authorization → users get a spurious
    # "reinstall this app" message) the moment it sees SLACK_CLIENT_ID /
    # SLACK_CLIENT_SECRET in the environment — which the QueryHub Web app
    # sets. This bot is single-workspace Socket Mode and must authorize with
    # the bot token, so strip those vars from THIS process's env before
    # constructing the App. (The web app runs separately and keeps them.)
    for _oauth_var in ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"):
        os.environ.pop(_oauth_var, None)
    app = App(token=ENV.slack_bot_token)
    handlers.register(app)

    # Re-submit any request left 'approved' by a hard crash (the in-memory
    # worker queue does not survive an ungraceful stop). The pool is empty at
    # boot, so this cannot double-run; _run re-authorizes and atomically
    # claims each one.
    resubmitted = executor.resubmit_approved_on_boot(app.client)
    if resubmitted:
        log.info("Re-submitted %d orphaned 'approved' request(s)", resubmitted)

    handler = SocketModeHandler(app, ENV.slack_app_token)

    # Scheduler daemon: polls every 60s for status='scheduled' requests
    # whose scheduled_for is due, flips them to 'executing', and submits.
    scheduler_stop = threading.Event()
    scheduler_thread = threading.Thread(
        target=executor.scheduler_loop,
        args=(app.client, scheduler_stop),
        kwargs={"interval_sec": 60},
        name="scheduler",
        daemon=True,
    )
    scheduler_thread.start()
    log.info("Scheduler thread started")

    # Auth-events daemon: drains auth_event_outbox (trigger-captured
    # authorization changes) into Slack DMs. See auth_events.py.
    auth_events_thread = threading.Thread(
        target=auth_events.poll_loop,
        args=(app.client, scheduler_stop),
        name="auth-events",
        daemon=True,
    )
    auth_events_thread.start()
    log.info("Auth-events thread started")

    stop = threading.Event()

    def _shutdown(signum, _frame):  # noqa: ANN001
        # Keep the signal handler tiny — enter drain + wake the main thread.
        # Draining (a bare bool) makes the submit path refuse NEW work at
        # once; in-flight queries are then allowed to finish in the teardown
        # below (executor.shutdown waits on the pool, bounded by systemd
        # TimeoutStopSec). The old version did the whole teardown here while
        # the main thread was blocked in start(), so the process hung; main()
        # now owns teardown and exits cleanly.
        lifecycle.begin_drain()
        log.info("Signal %s received, draining + shutting down...", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # connect() opens the Socket Mode connection WITHOUT blocking, leaving
    # the main thread free to wait on our own stop event. handler.start()
    # would instead block forever on Bolt's internal event, which close()
    # does not break — the root cause of the slow shutdown.
    handler.connect()
    log.info("⚡️ Bolt app is running!")
    try:
        notifications.dm_all_admins(
            app.client, ":arrows_counterclockwise: *QueryHub bot* restarted "
            "and is back online (Slack).")
    except Exception:
        log.exception("startup admin DM failed")
    try:
        while not stop.wait(timeout=1.0):
            pass
    finally:
        scheduler_stop.set()
        # No "stopping" DM here — fanning out to every admin would add
        # latency to a plain restart. The startup DM above already tells
        # admins a restart happened (the service came back). Drain is set in
        # the signal handler; here we just finish in-flight + close.
        try:
            handler.close()
        except Exception:
            log.exception("error closing the Socket Mode handler")
        executor.shutdown()   # waits for in-flight queries (bounded by TimeoutStopSec)
        db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
