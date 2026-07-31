-- Migration 048: p_metrics_csv_imports — reportable CSV-import facts view
--
-- One denormalized row per CSV import for the metrics dashboard, joined to
-- the target alias and minus rows whose REQUESTER is in
-- report_excluded_users (same self-test exclusion the other p_metrics_*
-- views apply). The dashboard does client-side aggregation, so this view
-- just denormalizes — no rollups here.

CREATE OR REPLACE VIEW p_metrics_csv_imports AS
SELECT
    ci.id,
    ci.created_at,
    ci.completed_at,
    ci.requester_slack_id,
    ci.requester_name,
    ci.target_server_id,
    ts.alias                                   AS target_alias,
    ci.database_name,
    ci.table_name,
    ci.is_new_table,
    ci.unlogged,
    ci.status,
    ci.row_count,
    ci.inserted_rows,
    ci.byte_size,
    -- Load duration in seconds (NULL until completed). Mirrors how the
    -- request facts view exposes timing for client-side charting.
    EXTRACT(EPOCH FROM (ci.completed_at - ci.executed_at))::numeric AS load_seconds
  FROM csv_imports ci
  LEFT JOIN target_servers ts ON ts.id = ci.target_server_id
 WHERE NOT EXISTS (
     SELECT 1 FROM report_excluded_users e
      WHERE e.slack_user_id = ci.requester_slack_id
 );

COMMENT ON VIEW p_metrics_csv_imports IS
$$One row per CSV import (`/sql import`), denormalized with the target alias
and minus rows whose requester is in `report_excluded_users`. Feeds the
dashboard's CSV-import section; aggregation happens client-side.$$;
