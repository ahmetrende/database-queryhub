-- Make a running query cancellable, and make a runaway self-healing.
--
-- Two things were missing, and one measured fact undermines an assumption the
-- existing safety net was built on.
--
-- 1. Nobody could stop a running query. A user watching their own query crawl
--    had no button, and an operator had to find the backend by hand in
--    pg_stat_activity. `requests` never recorded which server-side backend was
--    running the statement, so there was nothing to aim at.
--
-- 2. `reconcile_orphaned_executing()` only sweeps rows whose LEASE has expired
--    (execution_lease_sec, 900s), and it is right to be that patient: QueryHub
--    Web and the bot run separate executors against this control DB, so a row
--    that started 60s ago may belong to a healthy sibling process.
--
--    Its comment justifies the margin by saying the lease "sits well above the
--    max query lifetime (statement_timeout + result streaming)". That is FALSE,
--    and it was measured on 2026-07-28: a backend blocked in `Client/ClientWrite`
--    — the server trying to push results at a client that is not reading —
--    processes neither `statement_timeout` NOR `pg_cancel_backend`. A query with
--    a 300s timeout was observed still running at 578s; only
--    pg_terminate_backend ended it. So a runaway CAN outlive the lease, and
--    liveness of the owning process is not evidence that anything is healthy.
--
-- Hence: record the backend, and record an explicit cancel request.
--
-- `backend_pid` is the target-side pid, captured at execution start. It is only
-- meaningful together with the target and the connection that produced it, which
-- is why cancellation re-checks the pid still runs THIS request's query before
-- signalling it — a pid is reused after a backend exits, and signalling a
-- stranger's backend on a production database would be a serious bug.
--
-- `cancel_requested_at` / `cancel_requested_by` record intent separately from
-- outcome: the request may still complete normally if it finishes first, and the
-- audit trail should show that someone asked, whichever way it went.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS backend_pid INTEGER,
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancel_requested_by TEXT;

COMMENT ON COLUMN requests.backend_pid IS
    'Target-side backend pid running this statement, captured at execution '
    'start. Only valid while status = executing, and only for the target this '
    'request names — verify the pid is still running this query before '
    'signalling it, because pids are reused.';
COMMENT ON COLUMN requests.cancel_requested_at IS
    'When a cancel was asked for (by the requester or an admin). Intent, not '
    'outcome: the query may still finish normally.';

-- How long an execution may run before the watchdog treats it as a runaway.
-- Distinct from execution_lease_sec (900s, "is the owning process alive") and
-- from query_timeout_sec (300s, the server-side statement_timeout that the
-- ClientWrite case defeats). This one is wall-clock and unconditional: past it,
-- something is wrong regardless of who owns it.
--
-- Default 420s = query_timeout_sec (300) + 120s of result streaming and masking
-- headroom. Below execution_lease_sec on purpose, so the watchdog acts BEFORE
-- the orphan sweep would, and acts on the real backend instead of only
-- rewriting the row.
INSERT INTO bot_config (key, value, description) VALUES
('execution_runaway_sec', '420',
 'Wall-clock seconds after which an ''executing'' request is treated as a '
 'runaway: the watchdog cancels its target backend, escalates to terminate if '
 'the cancel does not land, and fails the request. Independent of '
 'execution_lease_sec (process liveness) because a backend blocked writing to '
 'a slow client honours neither statement_timeout nor pg_cancel_backend. Set 0 '
 'to disable.'),
('cancel_escalate_sec', '5',
 'Seconds to wait for pg_cancel_backend to take effect before escalating to '
 'pg_terminate_backend. A backend blocked in Client/ClientWrite never processes '
 'a cancel, so without escalation a cancel request would silently do nothing.')
ON CONFLICT (key) DO NOTHING;
