-- The handle a non-Postgres engine gives you for a running query.
--
-- Cancelling a Postgres query means signalling a backend pid, which is why
-- `backend_pid` (migration 084) exists. An engine reached over an API has no
-- pid: Athena hands back a QueryExecutionId, and stopping the query means
-- calling StopQueryExecution with it.
--
-- It has to be a COLUMN rather than something the executing process remembers,
-- because the process that runs the query is often not the process that
-- receives the cancel: a query submitted from the web UI executes in
-- queryhub-web, and its owner can cancel it from Slack, which is a different
-- process on the same host. An in-memory registry would answer "not running"
-- to exactly the cancel that matters.
--
-- Recorded the moment the query starts, not when it finishes -- the whole
-- point is to reach it while it is still running.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS engine_execution_id TEXT;

COMMENT ON COLUMN requests.engine_execution_id IS
  $$Engine-side handle for a running query on an engine that has no backend pid (athena: QueryExecutionId). Written at start; used to stop the query on cancel or timeout.$$;
