"""Stop a running query: user-requested cancel, and a runaway watchdog.

Both do the same thing to the database and differ only in who asked, so they
share one implementation.

Why this exists in the shape it does, from what was measured on 2026-07-28
against a production target:

* `statement_timeout` does NOT bound a query whose backend is blocked in
  `Client/ClientWrite` — the server pushing results at a client that has stopped
  reading. A query configured with a 300s timeout was still running at 578s.
* `pg_cancel_backend` does not reach that backend either. It returned true and
  changed nothing; the backend never processes the interrupt while blocked in the
  socket write.
* `pg_terminate_backend` ended it immediately.

So a cancel that is not prepared to escalate is a cancel that silently fails in
the one case where it matters most. Everything here escalates.

The safety property that matters: a pid is reused once a backend exits, so
signalling a pid recorded minutes ago could hit an unrelated session on a
production database. Every signal is therefore preceded by a check that the pid
is still running THIS request's query, on the connection the request names.
"""
from __future__ import annotations

import logging
import time

from . import audit, db, targets
from . import config as cfg

log = logging.getLogger(__name__)


class CancelOutcome:
    """What actually happened, so callers can tell the user the truth."""

    NOT_RUNNING = "not_running"        # nothing to signal (already finished)
    CANCELLED = "cancelled"            # pg_cancel_backend was enough
    TERMINATED = "terminated"          # needed pg_terminate_backend
    FAILED = "failed"                  # could not reach the target / no pid


def request_cancel(request_id: int, by_id: str | None,
                   by_name: str | None) -> None:
    """Record that a cancel was asked for. Intent, not outcome — the query may
    still finish normally, and the audit trail should show the ask either way."""
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET cancel_requested_at = NOW(), "
            "  cancel_requested_by = %s "
            " WHERE id = %s AND status = 'executing' "
            "   AND cancel_requested_at IS NULL",
            (by_id, request_id))
        audit.log_in(cur, request_id, by_id, by_name, "cancel_requested", {})


def _matches_this_request(cur, pid: int, request_id: int) -> bool:
    """Is `pid` still OUR backend for `request_id`?

    Identified by `application_name`, which the executor sets to
    `… req=<id> by=<who>` when it opens the connection. That is a property of the
    SESSION, so it holds whatever statement the backend happens to be running.

    It used to be identified by the query TEXT instead, and that cost an outage.
    A wedged execution had moved on to a follow-up catalog query, so the text no
    longer matched, the probe reported `not_running`, and the runaway watchdog
    marked the row failed while TERMINATING NOTHING — leaving the client blocked
    on a backend it had decided was gone. Terminating it is the one thing that
    unblocks that case (a backend stuck in ClientWrite honours neither
    statement_timeout nor pg_cancel_backend), and the check that was supposed to
    authorize it looked away at exactly the wrong moment.

    The query-text test survives as a fallback for a session with no
    application_name — a pre-084 execution, or a connection opened by something
    other than the executor.
    """
    row = db.fetch_one("SELECT query FROM requests WHERE id = %s", (request_id,))
    if not row or not row.get("query"):
        return False
    head = " ".join(row["query"].split())[:60]
    # `req=<id> ` with the trailing space so req=12 cannot match req=123. The
    # `by=` part always follows, and the 63-byte truncation only ever cuts the
    # email at the end, never this.
    app_marker = f"%req={request_id} %"
    # `state = 'active'`, not `state <> 'idle'`. The difference is not
    # cosmetic and it was caught by running this against a real backend: a
    # successfully cancelled statement leaves the session sitting in
    # `idle in transaction (aborted)`, which is NOT equal to 'idle' — so the
    # looser test reported the backend as still running, the escalation always
    # fired, and every cancel became a terminate. The polite path could never
    # happen. Only 'active' means a statement is executing right now.
    # Discard any cached statistics snapshot before looking. Harmless outside a
    # transaction, and the difference between a working escalation check and one
    # that always reports "still running" if this ever runs inside one.
    try:
        cur.execute("SELECT pg_stat_clear_snapshot()")
    except Exception:
        pass
    cur.execute(
        "SELECT 1 FROM pg_stat_activity "
        " WHERE pid = %s AND state = 'active' "
        "   AND (application_name LIKE %s "
        "        OR (COALESCE(application_name, '') = '' "
        "            AND regexp_replace(query, '\\s+', ' ', 'g') LIKE %s))",
        (pid, app_marker, f"%{head}%"))
    return cur.fetchone() is not None


def stop_backend(request_id: int) -> str:
    """Signal the target backend running `request_id`. Returns a CancelOutcome.

    Cancel first (polite: the query rolls back, the session survives), then
    terminate if the cancel did not land — which, per the module docstring, is
    exactly what happens in the case worth caring about.
    """
    row = db.fetch_one(
        "SELECT backend_pid, target_server_id, database_name, status::text AS st "
        "  FROM requests WHERE id = %s", (request_id,))
    if not row:
        return CancelOutcome.FAILED
    if row["st"] != "executing":
        return CancelOutcome.NOT_RUNNING
    pid = row.get("backend_pid")
    if not pid:
        # Executions started before migration 084, or a request that failed
        # before it could record its backend.
        log.warning("cancel: request %s has no backend_pid", request_id)
        return CancelOutcome.FAILED

    escalate_after = max(1, cfg.get_int("cancel_escalate_sec", 5))
    try:
        target = targets.get(row["target_server_id"])
        if target is None:
            return CancelOutcome.FAILED
        import psycopg
        dsn = (f"host={target.host} port={target.port} "
               f"dbname={row['database_name']} user={target.username} "
               f"password={targets.get_password(target.id)}")
        conn_kwargs = dict(cfg.target_ssl_kwargs())
        # autocommit, and it matters. `pg_stat_activity`'s backend-status data is
        # cached for the duration of a TRANSACTION, so polling it inside one
        # returns the same snapshot every time: the cancel landed within 0.3s
        # (measured) and the poll kept reporting the backend as active, so the
        # escalation always fired and every cancel became a terminate. Outside a
        # transaction each read is fresh. `pg_stat_clear_snapshot()` below is the
        # belt to this braces.
        with psycopg.connect(dsn, connect_timeout=10, autocommit=True,
                             **conn_kwargs) as conn:
            with conn.cursor() as cur:
                if not _matches_this_request(cur, pid, request_id):
                    # Either it finished on its own, or the pid now belongs to
                    # someone else. Both mean: do not signal.
                    return CancelOutcome.NOT_RUNNING

                cur.execute("SELECT pg_cancel_backend(%s)", (pid,))
                log.info("cancel: pg_cancel_backend(%s) for request %s",
                         pid, request_id)

                # Give the cancel a chance, then check whether it landed.
                deadline = time.monotonic() + escalate_after
                while time.monotonic() < deadline:
                    time.sleep(0.5)
                    if not _matches_this_request(cur, pid, request_id):
                        return CancelOutcome.CANCELLED

                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
                log.warning("cancel: pg_cancel_backend did not land for request "
                            "%s, escalated to pg_terminate_backend(%s)",
                            request_id, pid)
                return CancelOutcome.TERMINATED
    except Exception:
        log.exception("cancel: could not signal backend for request %s",
                      request_id)
        return CancelOutcome.FAILED


def _fail_request(request_id: int, note: str, actor: str | None,
                  actor_name: str | None, action: str) -> bool:
    """Move an 'executing' row to failed. Conditional on it still being
    'executing', so a query that finished in the meantime keeps its result."""
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'failed', completed_at = NOW(), "
            "  error_message = %s "
            " WHERE id = %s AND status = 'executing'",
            (note, request_id))
        changed = cur.rowcount == 1
        if changed:
            audit.log_in(cur, request_id, actor, actor_name, action,
                         {"note": note})
    return changed


def cancel(request_id: int, by_id: str | None, by_name: str | None) -> str:
    """User-requested cancel: record the ask, stop the backend, fail the row."""
    request_cancel(request_id, by_id, by_name)
    outcome = stop_backend(request_id)
    if outcome in (CancelOutcome.CANCELLED, CancelOutcome.TERMINATED):
        _fail_request(
            request_id,
            f"Cancelled by {by_name or by_id or 'a user'} while it was running.",
            by_id, by_name, "cancelled_running")
    return outcome


# States a request can be withdrawn from: it has not started running, so there
# is no backend to signal and nothing to escalate — just a row to close and an
# audit line to leave. Kept next to cancel() because "stop my request" is one
# button to the person pressing it, and splitting the two across modules is how
# they end up behaving differently.
WITHDRAWABLE = ("pending", "changes_requested", "approved", "scheduled")


def withdraw(request_id: int, by_id: str | None, by_name: str | None) -> bool:
    """Cancel a request that has not started executing. True if it moved.

    The UPDATE is conditional on the status it was read at, so losing a race
    against the executor claiming the row is a no-op rather than a request that
    is `cancelled` in the table while a query runs against production. False
    means "it started, or somebody else got there first" — the caller re-reads
    and decides, and for a running request that means cancel() instead.
    """
    with db.transaction() as cur:
        cur.execute(
            "UPDATE requests SET status = 'cancelled', completed_at = NOW(), "
            "  decision_reason = %s, decided_at = NOW() "
            " WHERE id = %s AND status = ANY(%s)",
            (f"Withdrawn by {by_name or by_id or 'the requester'}.",
             request_id, list(WITHDRAWABLE)))
        if cur.rowcount != 1:
            return False
        audit.log_in(cur, request_id, by_id, by_name, "withdrawn",
                     {"by": by_id})
        return True


def sweep_runaways() -> int:
    """Stop executions that have outrun the wall clock. Returns how many.

    Deliberately NOT the same check as `reconcile_orphaned_executing`, which asks
    "is the owning process still alive" and waits `execution_lease_sec` (900s)
    before touching anything — correct, because a sibling process may be running
    the query healthily.

    That sweep's own comment justified its patience by claiming the lease "sits
    well above the max query lifetime (statement_timeout + result streaming)".
    Measurement says otherwise: with the backend blocked in ClientWrite,
    statement_timeout never fires, so a runaway can outlive the lease and keeps
    holding a production connection while it does. This check is unconditional
    wall-clock: past `execution_runaway_sec`, something is wrong whoever owns it,
    and the remedy has to reach the actual backend rather than only rewrite the
    row.
    """
    limit = cfg.get_int("execution_runaway_sec", 420)
    if limit <= 0:
        return 0
    stuck = db.fetch_all(
        "SELECT id FROM requests "
        " WHERE status = 'executing' AND executed_at IS NOT NULL "
        "   AND executed_at < NOW() - make_interval(secs => %s)",
        (limit,))
    n = 0
    for r in stuck:
        outcome = stop_backend(r["id"])
        note = (f"Stopped after running longer than {limit}s "
                f"(execution_runaway_sec). The server-side statement timeout "
                f"does not fire while the database is blocked writing results, "
                f"so the backend was signalled directly ({outcome}).")
        if _fail_request(r["id"], note, "SYSTEM", "runaway watchdog",
                         "execution_runaway_stopped"):
            n += 1
            log.warning("runaway watchdog stopped request %s (%s)",
                        r["id"], outcome)
    return n
