-- Execution lease for orphaned-request reconciliation (STAB-01 / BUG-03).
--
-- reconcile_orphaned_executing() sweeps rows stuck in 'executing' to
-- 'failed'. QueryHub Web and the bot run SEPARATE executor processes against
-- this same control DB, so a blanket sweep on one process's boot would kill
-- a query still running healthily in the other. The reconciler now only
-- touches rows whose execution started more than execution_lease_sec ago:
-- a genuinely live query is younger than the lease; an orphan (its process
-- died, taking its connection and query with it) ages past it. The default
-- sits well above the max query lifetime (query_timeout_sec + result
-- streaming), so a real query is never mistaken for an orphan.
--
-- Runtime-effective (read on each boot + each scheduler tick).

INSERT INTO bot_config (key, value, description) VALUES
  ('execution_lease_sec', '900',
   'Seconds a request may sit in ''executing'' before the orphan reconciler treats it as dead and fails it. Must exceed the max real query lifetime (query_timeout_sec + result streaming). Guards against one executor process failing a query still running in the sibling process (bot vs web).')
ON CONFLICT (key) DO NOTHING;
