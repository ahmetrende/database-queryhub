-- Configurable lower bound for time-series reports.
--
-- The bot's pilot went live on 2026-05-05; everything before that is
-- either operator self-tests or empty. Daily / weekly views were
-- previously bounded by a rolling NOW() - 90 days, which showed
-- months of empty pre-pilot history.
--
-- This migration:
--   1. Adds a `report_start_date` bot_config knob (default 2026-05-01).
--   2. Replaces the 90-day rolling window in every time-axis
--      p_metrics_* view with `created_at >= report_start_date`.
--   3. Leaves cumulative-only views (cost_savings, team_usage,
--      top_users, admin_workload, target_heatmap) alone — they have
--      no time x-axis, so a lower bound would silently undercount.
--
-- To extend / shorten the window: UPDATE bot_config and reload the
-- dashboard. No view-definition churn.

INSERT INTO bot_config (key, value, description) VALUES
    ('report_start_date', '2026-05-01',
     'Lower bound (inclusive, YYYY-MM-DD) for daily / weekly p_metrics_* '
     'time-series views. Days / weeks before this date are excluded. '
     'Set to the pilot launch date.')
ON CONFLICT (key) DO NOTHING;

-- --------------------------------------------------------------------
-- Daily views — start the gap-filled series at report_start_date.
-- --------------------------------------------------------------------

CREATE OR REPLACE VIEW p_metrics_volume_daily AS
WITH days AS (
    SELECT generate_series(
              p_metrics_cfg_text('report_start_date', '2026-05-01')::date,
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
     WHERE created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
     GROUP BY 1
)
SELECT d.day,
       COALESCE(a.submitted,    0) AS submitted,
       COALESCE(a.completed,    0) AS completed,
       COALESCE(a.rejected,     0) AS rejected,
       COALESCE(a.failed,       0) AS failed,
       COALESCE(a.cancelled,    0) AS cancelled,
       COALESCE(a.active_users, 0) AS active_users
  FROM days d
  LEFT JOIN agg a USING (day)
 ORDER BY d.day DESC;

CREATE OR REPLACE VIEW p_metrics_usage_daily AS
WITH days AS (
    SELECT generate_series(
              p_metrics_cfg_text('report_start_date', '2026-05-01')::date,
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
     WHERE created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
     GROUP BY 1
),
ann AS (
    SELECT date_trunc('day', occurred_at)::date          AS day,
           string_agg(label, ' | ' ORDER BY occurred_at) AS annotations
      FROM metric_annotations
     WHERE occurred_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
     GROUP BY 1
)
SELECT d.day,
       COALESCE(daily.submitted,       0) AS submitted,
       COALESCE(daily.completed,       0) AS completed,
       COALESCE(daily.failed,          0) AS failed,
       COALESCE(daily.rejected,        0) AS rejected,
       COALESCE(daily.cancelled,       0) AS cancelled,
       COALESCE(daily.awaiting_dba,    0) AS awaiting_dba,
       COALESCE(daily.scheduled,       0) AS scheduled,
       COALESCE(daily.active_users,    0) AS active_users,
       COALESCE(daily.targets_touched, 0) AS targets_touched,
       COALESCE(daily.rows_returned,   0) AS rows_returned,
       daily.avg_exec_sec,
       daily.p95_exec_sec,
       daily.avg_approval_sec,
       ann.annotations
  FROM days d
  LEFT JOIN daily ON daily.day = d.day
  LEFT JOIN ann   ON ann.day   = d.day
 ORDER BY d.day DESC;

-- --------------------------------------------------------------------
-- Weekly / monthly aggregates — just add the WHERE; gap-fill not
-- needed because the underlying buckets are dense once they exist.
-- --------------------------------------------------------------------

CREATE OR REPLACE VIEW p_metrics_volume_weekly AS
SELECT date_trunc('week', created_at)::date             AS week,
       count(*)                                         AS submitted,
       count(*) FILTER (WHERE status = 'completed')     AS completed,
       count(*) FILTER (WHERE status = 'rejected')      AS rejected,
       count(*) FILTER (WHERE status = 'failed')        AS failed,
       count(*) FILTER (WHERE status = 'cancelled')     AS cancelled,
       count(DISTINCT requester_slack_id)               AS wau
  FROM requests_reportable
 WHERE created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_volume_monthly AS
SELECT date_trunc('month', created_at)::date            AS month,
       count(*)                                         AS submitted,
       count(*) FILTER (WHERE status = 'completed')     AS completed,
       count(*) FILTER (WHERE status = 'rejected')      AS rejected,
       count(*) FILTER (WHERE status = 'failed')        AS failed,
       count(*) FILTER (WHERE status = 'cancelled')     AS cancelled,
       count(DISTINCT requester_slack_id)               AS mau
  FROM requests_reportable
 WHERE created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_tier_distribution AS
WITH classified AS (
    SELECT date_trunc('week', created_at)::date AS week,
           CASE upper(split_part(btrim(query), ' ', 1))
                WHEN 'SELECT'  THEN 'ro'
                WHEN 'WITH'    THEN 'ro'
                WHEN 'EXPLAIN' THEN 'ro'
                WHEN 'SHOW'    THEN 'ro'
                WHEN 'VALUES'  THEN 'ro'
                WHEN 'TABLE'   THEN 'ro'
                WHEN 'INSERT'  THEN 'rw'
                WHEN 'UPDATE'  THEN 'rw'
                WHEN 'DELETE'  THEN 'rw'
                WHEN 'MERGE'   THEN 'rw'
                ELSE 'ddl_or_other'
           END AS tier
      FROM requests_reportable
     WHERE created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
)
SELECT week,
       count(*) FILTER (WHERE tier = 'ro')           AS ro,
       count(*) FILTER (WHERE tier = 'rw')           AS rw,
       count(*) FILTER (WHERE tier = 'ddl_or_other') AS ddl_or_other,
       count(*)                                      AS total
  FROM classified
 GROUP BY week ORDER BY week DESC;

CREATE OR REPLACE VIEW p_metrics_failure_breakdown AS
SELECT date_trunc('week', completed_at)::date        AS week,
       count(*) FILTER (WHERE status = 'completed') AS completed,
       count(*) FILTER (WHERE status = 'rejected')  AS admin_rejected,
       count(*) FILTER (WHERE status = 'failed')    AS execute_failed,
       count(*) FILTER (WHERE status = 'cancelled') AS user_cancelled,
       count(*)                                     AS terminal_total,
       round(100.0 * count(*) FILTER (WHERE status = 'completed')::numeric
             / NULLIF(count(*), 0)::numeric, 1)     AS success_pct
  FROM requests_reportable
 WHERE completed_at IS NOT NULL
   AND completed_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_scheduled_usage AS
SELECT date_trunc('week', created_at)::date           AS week,
       count(*)                                       AS total_requests,
       count(*) FILTER (WHERE scheduled_for IS NOT NULL) AS scheduled,
       round(100.0 * count(*) FILTER (WHERE scheduled_for IS NOT NULL)::numeric
             / NULLIF(count(*), 0)::numeric, 1)       AS scheduled_pct,
       count(*) FILTER (WHERE scheduled_for IS NOT NULL AND status = 'completed') AS scheduled_completed,
       count(*) FILTER (WHERE scheduled_for IS NOT NULL AND status = 'cancelled') AS scheduled_cancelled
  FROM requests_reportable
 WHERE created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_business_vs_offhours AS
WITH classified AS (
    SELECT created_at,
           created_at AT TIME ZONE p_metrics_cfg_text('report_timezone', 'UTC') AS local_ts,
           status
      FROM requests_reportable
     WHERE created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
)
SELECT date_trunc('week', created_at)::date AS week,
       count(*)                              AS total,
       count(*) FILTER (
           WHERE extract(dow FROM local_ts) BETWEEN 1 AND 5
             AND extract(hour FROM local_ts) BETWEEN 9 AND 17)        AS business_hours,
       count(*) FILTER (
           WHERE NOT (extract(dow FROM local_ts) BETWEEN 1 AND 5
                  AND extract(hour FROM local_ts) BETWEEN 9 AND 17))  AS off_hours,
       count(*) FILTER (WHERE extract(dow FROM local_ts) IN (0,6))   AS weekend,
       count(*) FILTER (WHERE extract(dow FROM local_ts) IN (1,2,3,4,5)
                          AND extract(hour FROM local_ts) >= 18)      AS weekday_evening,
       count(*) FILTER (WHERE extract(dow FROM local_ts) IN (1,2,3,4,5)
                          AND extract(hour FROM local_ts) <  9)       AS weekday_early,
       round(100.0 * count(*) FILTER (
                 WHERE NOT (extract(dow FROM local_ts) BETWEEN 1 AND 5
                        AND extract(hour FROM local_ts) BETWEEN 9 AND 17))::numeric
             / NULLIF(count(*), 0)::numeric, 1)                       AS off_hours_pct
  FROM classified
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_approval_sla AS
SELECT date_trunc('week', created_at)::date         AS week,
       count(*)                                     AS decided,
       round(percentile_cont(0.50) WITHIN GROUP (
             ORDER BY extract(epoch FROM decided_at - created_at))::numeric, 0) AS p50_sec,
       round(percentile_cont(0.90) WITHIN GROUP (
             ORDER BY extract(epoch FROM decided_at - created_at))::numeric, 0) AS p90_sec,
       round(percentile_cont(0.95) WITHIN GROUP (
             ORDER BY extract(epoch FROM decided_at - created_at))::numeric, 0) AS p95_sec,
       count(*) FILTER (WHERE extract(epoch FROM decided_at - created_at) <=  300) AS within_5min,
       count(*) FILTER (WHERE extract(epoch FROM decided_at - created_at) <= 1800) AS within_30min,
       count(*) FILTER (WHERE extract(epoch FROM decided_at - created_at) <= 3600) AS within_1h,
       count(*) FILTER (WHERE extract(epoch FROM decided_at - created_at) >  3600) AS over_1h
  FROM requests_reportable
 WHERE decided_at IS NOT NULL
   AND created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_peak_hours AS
WITH local_ts AS (
    SELECT created_at,
           created_at AT TIME ZONE p_metrics_cfg_text('report_timezone', 'UTC')
                                                                    AS local_created_at,
           status
      FROM requests_reportable
     WHERE created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
)
SELECT extract(dow FROM local_created_at)::int AS dow,
       to_char(local_created_at, 'Dy')        AS day_name,
       extract(hour FROM local_created_at)::int AS hour,
       count(*)                                AS requests,
       count(*) FILTER (WHERE status = 'completed') AS completed,
       count(DISTINCT date_trunc('day', local_created_at)) AS days_with_activity
  FROM local_ts
 GROUP BY 1, 2, 3
 ORDER BY 1, 3;

CREATE OR REPLACE VIEW p_metrics_rating_weekly AS
SELECT date_trunc('week', rated_at)::date          AS week,
       count(*)                                    AS n_ratings,
       round(avg(rating), 2)                       AS avg_rating,
       count(*) FILTER (WHERE rating <= 2)         AS low_count,
       count(*) FILTER (WHERE rating >= 4)         AS high_count,
       count(feedback_text)                        AS with_feedback
  FROM request_ratings_reportable
 WHERE rated_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_rating_response_rate AS
SELECT date_trunc('week', r.completed_at)::date    AS week,
       count(*)                                    AS terminal_requests,
       count(rr.*)                                 AS rated,
       round(count(rr.*)::numeric * 100
             / NULLIF(count(*), 0)::numeric, 1)    AS response_pct
  FROM requests_reportable r
  LEFT JOIN request_ratings_reportable rr ON rr.request_id = r.id
 WHERE r.status IN ('completed','failed','rejected','cancelled')
   AND r.completed_at IS NOT NULL
   AND r.completed_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date
 GROUP BY 1 ORDER BY 1 DESC;
