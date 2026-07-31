-- Test-traffic exclusion for product-metric reporting.
--
-- An operator's own self-test requests would otherwise pollute every
-- p_metrics_* view (volume, top users, admin workload, rating stats,
-- cost savings). We add a small allowlist-of-exclusions table:
-- everything else still sees the raw `requests` / `audit_log` /
-- `request_ratings` tables, but the reporting views consult three
-- thin wrapper views that filter out excluded users on both sides
-- of every request (requester + approver).
--
-- The bot itself does NOT consult this table — kill-switch, allow-
-- list, team grants, admin scope: all unchanged. Only the
-- p_metrics_* views and any future reporting hook should read from
-- the *_reportable wrappers.

-- ----------------------------------------------------------------------
-- 1. Exclusion registry.
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS report_excluded_users (
    slack_user_id TEXT         PRIMARY KEY
                  CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'),
    reason        TEXT,
    added_by      TEXT,
    added_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE report_excluded_users IS
$$Slack user IDs whose `requests` and `audit_log` activity is hidden from `p_metrics_*` views (via the `*_reportable` wrappers). Intended for operator self-tests; never affects bot authz or audit. To re-include a user, DELETE their row.$$;

-- ----------------------------------------------------------------------
-- 2. Reportable wrappers — these are what every p_metrics_* view
--    reads from.
-- ----------------------------------------------------------------------

CREATE OR REPLACE VIEW requests_reportable AS
SELECT r.*
  FROM requests r
 WHERE NOT EXISTS (
     SELECT 1 FROM report_excluded_users e
      WHERE e.slack_user_id = r.requester_slack_id
 );

COMMENT ON VIEW requests_reportable IS
$$`requests` minus rows whose REQUESTER is in `report_excluded_users`. Filter is by requester only — an excluded user approving someone else's request still keeps that real request in the metrics. The excluded user's own admin actions are filtered separately via `audit_log_reportable`.$$;

CREATE OR REPLACE VIEW audit_log_reportable AS
SELECT al.*
  FROM audit_log al
 WHERE NOT EXISTS (
     SELECT 1 FROM report_excluded_users e
      WHERE e.slack_user_id = al.actor_slack_id
 );

COMMENT ON VIEW audit_log_reportable IS
$$`audit_log` minus rows where actor_slack_id is in `report_excluded_users`. Used by p_metrics_admin_workload.$$;

CREATE OR REPLACE VIEW request_ratings_reportable AS
SELECT rr.*
  FROM request_ratings rr
 WHERE NOT EXISTS (
     SELECT 1 FROM report_excluded_users e
      WHERE e.slack_user_id = rr.slack_user_id
 )
   AND EXISTS (
     SELECT 1 FROM requests_reportable r WHERE r.id = rr.request_id
 );

COMMENT ON VIEW request_ratings_reportable IS
$$`request_ratings` minus ratings whose rater is excluded AND whose underlying request is itself excluded. Used by the p_metrics_rating_* views.$$;

-- ----------------------------------------------------------------------
-- 3. Rewrite every p_metrics_* view that aggregates request/audit
--    data to use the *_reportable wrappers. Verbatim definitions
--    with the base tables swapped in.
-- ----------------------------------------------------------------------

CREATE OR REPLACE VIEW p_metrics_cost_savings AS
WITH counts AS (
    SELECT count(*) FILTER (WHERE created_at >= date_trunc('month', NOW())) AS req_this_month,
           count(*) FILTER (WHERE created_at >= date_trunc('week',  NOW())) AS req_this_week,
           count(*) FILTER (WHERE created_at >= date_trunc('day',   NOW())) AS req_today,
           count(*)                                                         AS req_lifetime
      FROM requests_reportable
), cfg AS (
    SELECT p_metrics_cfg_num('cost_dba_minutes_per_request') AS dba_min,
           p_metrics_cfg_num('cost_dba_hourly_usd')          AS dba_hr,
           p_metrics_cfg_num('cost_avoided_replicas')        AS n_replicas,
           p_metrics_cfg_num('cost_per_replica_monthly_usd') AS per_replica,
           p_metrics_cfg_num('cost_other_monthly_usd')       AS other
)
SELECT c.req_this_month, c.req_this_week, c.req_today, c.req_lifetime,
       cfg.dba_min      AS dba_min_per_request,
       cfg.dba_hr       AS dba_hourly_usd,
       cfg.n_replicas   AS avoided_replicas,
       cfg.per_replica  AS per_replica_monthly_usd,
       cfg.other        AS other_monthly_usd,
       round(c.req_this_month::numeric * cfg.dba_min / 60.0 * cfg.dba_hr, 2) AS dba_saving_this_month_usd,
       round(c.req_lifetime::numeric  * cfg.dba_min / 60.0 * cfg.dba_hr, 2) AS dba_saving_lifetime_usd,
       round(cfg.n_replicas * cfg.per_replica, 2) AS replica_saving_monthly_usd,
       cfg.other AS other_saving_monthly_usd,
       round(c.req_this_month::numeric * cfg.dba_min / 60.0 * cfg.dba_hr
             + cfg.n_replicas * cfg.per_replica + cfg.other, 2) AS total_saving_this_month_usd
  FROM counts c CROSS JOIN cfg;

CREATE OR REPLACE VIEW p_metrics_volume_daily AS
SELECT date_trunc('day', created_at)::date              AS day,
       count(*)                                         AS submitted,
       count(*) FILTER (WHERE status = 'completed')     AS completed,
       count(*) FILTER (WHERE status = 'rejected')      AS rejected,
       count(*) FILTER (WHERE status = 'failed')        AS failed,
       count(*) FILTER (WHERE status = 'cancelled')     AS cancelled,
       count(DISTINCT requester_slack_id)               AS active_users
  FROM requests_reportable
 WHERE created_at >= NOW() - INTERVAL '90 days'
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_volume_weekly AS
SELECT date_trunc('week', created_at)::date             AS week,
       count(*)                                         AS submitted,
       count(*) FILTER (WHERE status = 'completed')     AS completed,
       count(*) FILTER (WHERE status = 'rejected')      AS rejected,
       count(*) FILTER (WHERE status = 'failed')        AS failed,
       count(*) FILTER (WHERE status = 'cancelled')     AS cancelled,
       count(DISTINCT requester_slack_id)               AS wau
  FROM requests_reportable
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
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_usage_daily AS
WITH daily AS (
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
SELECT d.day, d.submitted, d.completed, d.failed, d.rejected, d.cancelled,
       d.awaiting_dba, d.scheduled, d.active_users, d.targets_touched,
       d.rows_returned, d.avg_exec_sec, d.p95_exec_sec, d.avg_approval_sec,
       a.annotations
  FROM daily d LEFT JOIN ann a USING (day)
 ORDER BY d.day DESC;

CREATE OR REPLACE VIEW p_metrics_team_usage AS
SELECT t.name AS team,
       count(DISTINCT r.requester_slack_id)                AS active_users,
       count(*)                                            AS total_requests,
       count(*) FILTER (WHERE r.status = 'completed')      AS completed,
       count(*) FILTER (WHERE r.status = 'rejected')       AS rejected,
       count(*) FILTER (WHERE r.status = 'failed')         AS failed,
       round(avg(extract(epoch FROM r.completed_at - r.executed_at)), 1)
                                                           AS avg_exec_seconds,
       max(r.created_at)                                   AS last_request_at
  FROM requests_reportable r
  JOIN team_members tm ON tm.slack_user_id = r.requester_slack_id
  JOIN teams        t  ON t.id             = tm.team_id
 GROUP BY t.name
 ORDER BY count(*) DESC;

CREATE OR REPLACE VIEW p_metrics_top_users AS
SELECT r.requester_slack_id,
       coalesce(rq.name, r.requester_name, '(?)') AS name,
       rq.email,
       count(*)                                                  AS total_requests,
       count(*) FILTER (WHERE r.created_at >= NOW() - INTERVAL '30 days') AS last_30d,
       count(*) FILTER (WHERE r.created_at >= NOW() - INTERVAL '7 days')  AS last_7d,
       count(*) FILTER (WHERE r.status = 'completed')           AS completed,
       count(*) FILTER (WHERE r.status = 'rejected')            AS rejected,
       max(r.created_at)                                         AS last_request_at
  FROM requests_reportable r
  LEFT JOIN requesters rq ON rq.slack_user_id = r.requester_slack_id
 GROUP BY r.requester_slack_id, rq.name, r.requester_name, rq.email
 ORDER BY count(*) DESC;

CREATE OR REPLACE VIEW p_metrics_scheduled_usage AS
SELECT date_trunc('week', created_at)::date           AS week,
       count(*)                                       AS total_requests,
       count(*) FILTER (WHERE scheduled_for IS NOT NULL) AS scheduled,
       round(100.0 * count(*) FILTER (WHERE scheduled_for IS NOT NULL)::numeric
             / NULLIF(count(*), 0)::numeric, 1)       AS scheduled_pct,
       count(*) FILTER (WHERE scheduled_for IS NOT NULL AND status = 'completed') AS scheduled_completed,
       count(*) FILTER (WHERE scheduled_for IS NOT NULL AND status = 'cancelled') AS scheduled_cancelled
  FROM requests_reportable
 WHERE created_at >= NOW() - INTERVAL '90 days'
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
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_admin_workload AS
WITH actions AS (
    SELECT actor_slack_id, actor_name, action, created_at
      FROM audit_log_reportable
     WHERE action IN ('approved', 'rejected', 'changes_requested')
       AND actor_slack_id IS NOT NULL
)
SELECT actor_slack_id,
       max(actor_name)                                     AS name,
       count(*)                                            AS total_actions,
       count(*) FILTER (WHERE action = 'approved')         AS approved,
       count(*) FILTER (WHERE action = 'rejected')         AS rejected,
       count(*) FILTER (WHERE action = 'changes_requested') AS changes_requested,
       count(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS last_7d,
       count(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS last_30d,
       max(created_at)                                     AS last_action_at
  FROM actions
 GROUP BY actor_slack_id
 ORDER BY count(*) DESC;

CREATE OR REPLACE VIEW p_metrics_target_heatmap AS
SELECT ts.alias                                                   AS target,
       ts.enabled,
       coalesce(count(r.id), 0)                                   AS total_requests,
       count(r.id) FILTER (WHERE r.created_at >= NOW() - INTERVAL '7 days')  AS last_7d,
       count(r.id) FILTER (WHERE r.created_at >= NOW() - INTERVAL '30 days') AS last_30d,
       count(DISTINCT r.requester_slack_id)                       AS unique_users,
       count(r.id) FILTER (WHERE r.status = 'completed')          AS completed,
       count(r.id) FILTER (WHERE r.status = 'failed')             AS failed,
       count(r.id) FILTER (WHERE r.status = 'rejected')           AS rejected,
       max(r.created_at)                                          AS last_request_at
  FROM target_servers ts
  LEFT JOIN requests_reportable r ON r.target_server_id = ts.id
 GROUP BY ts.id, ts.alias, ts.enabled
 ORDER BY coalesce(count(r.id), 0) DESC, ts.alias;

CREATE OR REPLACE VIEW p_metrics_peak_hours AS
WITH local_ts AS (
    SELECT created_at,
           created_at AT TIME ZONE p_metrics_cfg_text('report_timezone', 'UTC')
                                                                    AS local_created_at,
           status
      FROM requests_reportable
     WHERE created_at >= NOW() - INTERVAL '90 days'
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

CREATE OR REPLACE VIEW p_metrics_business_vs_offhours AS
WITH classified AS (
    SELECT created_at,
           created_at AT TIME ZONE p_metrics_cfg_text('report_timezone', 'UTC') AS local_ts,
           status
      FROM requests_reportable
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
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_rating_weekly AS
SELECT date_trunc('week', rated_at)::date          AS week,
       count(*)                                    AS n_ratings,
       round(avg(rating), 2)                       AS avg_rating,
       count(*) FILTER (WHERE rating <= 2)         AS low_count,
       count(*) FILTER (WHERE rating >= 4)         AS high_count,
       count(feedback_text)                        AS with_feedback
  FROM request_ratings_reportable
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
 GROUP BY 1 ORDER BY 1 DESC;

CREATE OR REPLACE VIEW p_metrics_rating_low_with_feedback AS
SELECT rr.rated_at,
       rr.rating,
       rr.feedback_text,
       r.id                AS request_id,
       r.requester_name,
       r.requester_slack_id,
       r.target_server_id,
       r.status,
       left(r.query, 200)  AS query_preview
  FROM request_ratings_reportable rr
  JOIN requests_reportable        r  ON r.id = rr.request_id
 WHERE rr.rating <= 2
 ORDER BY rr.rated_at DESC;

-- ----------------------------------------------------------------------
-- No seed shipped — the operator's own slack id is environment-specific
-- and would leak as a hardcoded value in a public repo. After applying
-- this migration, add yourself (and any future test accounts) via the
-- SQL snippet in docs/OPERATIONS.md §22:
--
--   INSERT INTO report_excluded_users (slack_user_id, reason, added_by)
--   VALUES ('U0XXXXXXXXX', 'Operator self-test traffic', 'U0XXXXXXXXX')
--   ON CONFLICT (slack_user_id) DO NOTHING;
-- ----------------------------------------------------------------------
