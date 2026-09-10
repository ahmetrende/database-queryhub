-- Keep a target's traffic out of product metrics, the way a person's already is.
--
-- `report_excluded_users` exists because the operator's own self-test queries
-- are not product usage and would flatter every number they appear in. The same
-- is true of a whole TARGET: the control-plane RDS is the operator's own box,
-- and queries against it are administration, not somebody using the product to
-- get their work done.
--
-- Deliberately narrow. This excludes rows from the *_reportable views, which
-- feed metrics and the dashboard. It does NOT touch the audit trail: that reads
-- `audit_log` directly and must keep showing everything, for exactly the reason
-- the audit screen was rebuilt this week -- a trail that omits a class of rows
-- is not a trail. Excluding traffic from a chart and hiding it from an auditor
-- are different acts and only one of them is wanted here.

CREATE TABLE IF NOT EXISTS report_excluded_targets (
    target_server_id integer PRIMARY KEY
        REFERENCES target_servers (id) ON DELETE CASCADE,
    reason           text,
    added_by         text,
    added_at         timestamptz NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE report_excluded_targets IS
    'Targets whose traffic is administration rather than product usage. Read by '
    'the *_reportable views only -- never by the audit trail.';

CREATE OR REPLACE VIEW requests_reportable AS
    SELECT id, requester_slack_id, requester_name, target_server_id,
           database_name, query, wants_result, justification, status,
           decided_by_slack_id, decided_by_name, decision_reason, decided_at,
           executed_at, completed_at, row_count, truncated, error_message,
           csv_file_path, created_at, slack_file_id, scheduled_for,
           requester_dm_channel_id, requester_dm_message_ts, explain_plan,
           bundle_id, "position", result_format, risk_summary,
           query_fingerprint, origin
      FROM requests r
     WHERE status <> 'draft'::request_status
       AND NOT EXISTS (SELECT 1 FROM report_excluded_users e
                        WHERE e.slack_user_id = r.requester_slack_id)
       AND NOT EXISTS (SELECT 1 FROM report_excluded_targets t
                        WHERE t.target_server_id = r.target_server_id);
