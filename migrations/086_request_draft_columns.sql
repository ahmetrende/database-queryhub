-- Make a draft row storable, and keep every other row exactly as strict.
--
-- A draft has no target, no database and no SQL yet — it is a reserved id and
-- nothing else. `target_server_id` is a foreign key to target_servers, so there
-- is no sentinel value available: it has to become nullable. `query` and
-- `database_name` are plain text and stay NOT NULL, holding '' for a draft.
--
-- Loosening a NOT NULL on a table this size is exactly the kind of change that
-- quietly permits bad data later, so the guarantee is restored by a CHECK for
-- every status except 'draft'. Verified before applying: 0 of the 1940 existing
-- rows violate it.
ALTER TABLE requests ALTER COLUMN target_server_id DROP NOT NULL;

ALTER TABLE requests DROP CONSTRAINT IF EXISTS requests_complete_unless_draft;
ALTER TABLE requests ADD CONSTRAINT requests_complete_unless_draft CHECK (
    status = 'draft'
    OR (target_server_id IS NOT NULL AND query <> '' AND database_name <> '')
);

-- One filter covers the whole reporting surface. Measured rather than assumed:
-- all 19 views that touch requests (18 p_metrics_* plus request_ratings_reportable
-- and audit_log_reportable) read it through THIS view, so excluding drafts here
-- keeps them out of the dashboard, the volume counts and the SLA figures without
-- touching any of them. A draft is not a request anybody made; counting one as
-- submitted work would inflate every volume number by however many tabs people
-- happen to open.
--
-- The rest of the definition is reproduced as-is (the operator self-test
-- exclusion); only the status filter is new.
CREATE OR REPLACE VIEW requests_reportable AS
SELECT r.id, r.requester_slack_id, r.requester_name, r.target_server_id,
       r.database_name, r.query, r.wants_result, r.justification, r.status,
       r.decided_by_slack_id, r.decided_by_name, r.decision_reason, r.decided_at,
       r.executed_at, r.completed_at, r.row_count, r.truncated, r.error_message,
       r.csv_file_path, r.created_at, r.slack_file_id, r.scheduled_for,
       r.requester_dm_channel_id, r.requester_dm_message_ts, r.explain_plan,
       r.bundle_id, r."position", r.result_format, r.risk_summary,
       r.query_fingerprint, r.origin
  FROM requests r
 WHERE r.status <> 'draft'
   AND NOT EXISTS (
        SELECT 1 FROM report_excluded_users e
         WHERE e.slack_user_id = r.requester_slack_id);

-- Abandoned drafts are the expected case, not an error: people open tabs and
-- close them. A partial index keeps the reaper's scan proportional to the
-- drafts, not to the whole table.
CREATE INDEX IF NOT EXISTS requests_draft_created_idx
    ON requests (created_at) WHERE status = 'draft';

INSERT INTO bot_config (key, value, description) VALUES
  ('draft_request_ttl_hours', '24',
   'Delete draft requests (reserved ids from opened query tabs) never submitted '
   'within this many hours. 0 disables the cleanup.')
ON CONFLICT (key) DO NOTHING;
