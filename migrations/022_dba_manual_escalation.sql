-- DDL escalation path: when the bot's queryhub_ddl user lacks
-- ownership / superuser to execute a DDL query, the executor catches
-- Postgres's 42501 (insufficient_privilege) and routes the request to
-- a new "awaiting_dba_manual" state instead of marking it failed.
-- A human DBA then runs the query out-of-band and clicks one of two
-- buttons on the admin DM (Mark completed / Mark failed), which
-- transitions the request to its final state and notifies the user.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
          JOIN pg_enum e ON e.enumtypid = t.oid
         WHERE t.typname = 'request_status' AND e.enumlabel = 'awaiting_dba_manual'
    ) THEN
        ALTER TYPE request_status ADD VALUE 'awaiting_dba_manual';
    END IF;
END $$;

COMMENT ON TYPE request_status IS
$$Lifecycle for a /sql request. Terminal states: completed, failed, rejected, cancelled. Transient: pending, changes_requested, approved, scheduled, executing, awaiting_dba_manual (DDL that needs DBA elevation to run).$$;
