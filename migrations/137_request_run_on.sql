-- `requests.run_on`: where a super-admin asked this request to run.
--
-- INTENT, not authority -- the same shape as `requests.unmasked` (093). The
-- executor re-derives at run time whether the choice still applies: a
-- requester who is no longer a super-admin runs as if they had not chosen, and
-- the automatic rules in replicas.py decide.
--
--   NULL            auto: replicas.py decides, exactly as before this column
--   'primary'       never a read replica
--   'replica:<id>'  that read replica (target_servers.id) of the request's own
--                   target -- or the request FAILS. A query somebody sent to a
--                   replica on purpose never runs on the primary instead.
--
-- Every existing row is NULL, so nothing that already ran reads differently.
-- Each honoured choice also writes an audit_log row (`execution_run_on_forced`)
-- saying what was asked for, where it ran and how far behind the replica was.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS run_on TEXT;

-- The executor parses this value, so the shapes it understands are the only
-- ones the table accepts. The column is new and empty, so the check has
-- nothing to scan.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'requests_run_on_shape') THEN
        ALTER TABLE requests ADD CONSTRAINT requests_run_on_shape
            CHECK (run_on IS NULL
                   OR run_on = 'primary'
                   OR run_on ~ '^replica:[1-9][0-9]*$');
    END IF;
END $$;

COMMENT ON COLUMN requests.run_on IS
  $$Where a super-admin asked this request to run: NULL = auto (replicas.py decides), 'primary', or 'replica:<target_servers.id>'. Intent only: the executor re-checks super-admin standing at run time and ignores the choice when it is gone.$$;
