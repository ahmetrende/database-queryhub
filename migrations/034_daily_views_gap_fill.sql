-- Gap-fill the daily-bucket reporting views.
--
-- Previously p_metrics_volume_daily / p_metrics_usage_daily only
-- emitted rows for days that had at least one request. That created
-- holes in the x-axis (e.g. a quiet Saturday with no traffic was
-- invisible) so the dashboard's "shade weekends" pass would see only
-- one of two weekend days and render a single-column band where a
-- two-column band belongs.
--
-- Fix: LEFT JOIN every aggregate against a generate_series of dates,
-- so every day in the last 90 days has a row (with zeros for the
-- missing days). Weekly / monthly views and the heatmap aren't
-- affected — every week / month / dow-hour bucket is dense by
-- definition.

CREATE OR REPLACE VIEW p_metrics_volume_daily AS
WITH days AS (
    SELECT generate_series(
              date_trunc('day', NOW() - INTERVAL '90 days')::date,
              date_trunc('day', NOW())::date,
              '1 day'::interval
           )::date AS day
),
agg AS (
    SELECT date_trunc('day', created_at)::date          AS day,
           count(*)                                     AS submitted,
           count(*) FILTER (WHERE status = 'completed') AS completed,
           count(*) FILTER (WHERE status = 'rejected')  AS rejected,
           count(*) FILTER (WHERE status = 'failed')    AS failed,
           count(*) FILTER (WHERE status = 'cancelled') AS cancelled,
           count(DISTINCT requester_slack_id)           AS active_users
      FROM requests_reportable
     WHERE created_at >= NOW() - INTERVAL '90 days'
     GROUP BY 1
)
SELECT d.day,
       COALESCE(a.submitted,     0) AS submitted,
       COALESCE(a.completed,     0) AS completed,
       COALESCE(a.rejected,      0) AS rejected,
       COALESCE(a.failed,        0) AS failed,
       COALESCE(a.cancelled,     0) AS cancelled,
       COALESCE(a.active_users,  0) AS active_users
  FROM days d
  LEFT JOIN agg a USING (day)
 ORDER BY d.day DESC;

CREATE OR REPLACE VIEW p_metrics_usage_daily AS
WITH days AS (
    -- Usage daily is "all-time" (no 90d cap) — anchor the lower bound
    -- to the earliest request so we don't generate decades of zero
    -- rows on a brand-new install.
    SELECT generate_series(
              COALESCE(date_trunc('day', (SELECT min(created_at) FROM requests_reportable))::date,
                       date_trunc('day', NOW())::date),
              date_trunc('day', NOW())::date,
              '1 day'::interval
           )::date AS day
),
daily AS (
    SELECT date_trunc('day', created_at)::date          AS day,
           count(*)                                     AS submitted,
           count(*) FILTER (WHERE status = 'completed') AS completed,
           count(*) FILTER (WHERE status = 'failed')    AS failed,
           count(*) FILTER (WHERE status = 'rejected')  AS rejected,
           count(*) FILTER (WHERE status = 'cancelled') AS cancelled,
           count(*) FILTER (WHERE status = 'awaiting_dba_manual') AS awaiting_dba,
           count(*) FILTER (WHERE scheduled_for IS NOT NULL) AS scheduled,
           count(DISTINCT requester_slack_id)           AS active_users,
           count(DISTINCT target_server_id)             AS targets_touched,
           sum(coalesce(row_count, 0))                  AS rows_returned,
           round(avg(extract(epoch FROM (completed_at - executed_at)))
                 FILTER (WHERE completed_at IS NOT NULL
                           AND executed_at  IS NOT NULL)::numeric, 2)
                                                        AS avg_exec_sec,
           round((percentile_cont(0.95) WITHIN GROUP (
                     ORDER BY extract(epoch FROM (completed_at - executed_at))
                 ) FILTER (WHERE completed_at IS NOT NULL
                             AND executed_at  IS NOT NULL))::numeric, 2)
                                                        AS p95_exec_sec,
           round(avg(extract(epoch FROM (decided_at - created_at)))
                 FILTER (WHERE decided_at IS NOT NULL)::numeric, 2)
                                                        AS avg_approval_sec
      FROM requests_reportable
     GROUP BY 1
),
ann AS (
    SELECT date_trunc('day', occurred_at)::date          AS day,
           string_agg(label, ' | ' ORDER BY occurred_at) AS annotations
      FROM metric_annotations
     GROUP BY 1
)
SELECT d.day,
       COALESCE(daily.submitted,        0) AS submitted,
       COALESCE(daily.completed,        0) AS completed,
       COALESCE(daily.failed,           0) AS failed,
       COALESCE(daily.rejected,         0) AS rejected,
       COALESCE(daily.cancelled,        0) AS cancelled,
       COALESCE(daily.awaiting_dba,     0) AS awaiting_dba,
       COALESCE(daily.scheduled,        0) AS scheduled,
       COALESCE(daily.active_users,     0) AS active_users,
       COALESCE(daily.targets_touched,  0) AS targets_touched,
       COALESCE(daily.rows_returned,    0) AS rows_returned,
       daily.avg_exec_sec,
       daily.p95_exec_sec,
       daily.avg_approval_sec,
       ann.annotations
  FROM days d
  LEFT JOIN daily ON daily.day = d.day
  LEFT JOIN ann   ON ann.day   = d.day
 ORDER BY d.day DESC;
