-- Product metric views (p_metrics_*). Read-only aggregations over the
-- bot's own tables. Safe to expose to anyone with SELECT on the bot DB.
--
-- All cost / savings numbers are driven by tunable bot_config rows
-- (cost_*) so each org can set its own DBA rates and infra costs
-- without rewriting views.

-- ---------- 1. Cost-assumption knobs (edit to tune) ----------

INSERT INTO bot_config (key, value, description) VALUES
    ('cost_dba_minutes_per_request', '8',
     'Estimated DBA minutes saved per /sql request — the alternative is a Slack DM / ticket loop that costs about this much per touch. Used by p_metrics_cost_savings.'),
    ('cost_dba_hourly_usd', '75',
     'Fully-loaded DBA hourly cost in USD (salary + overhead). Used to convert minutes_saved into dollar savings.'),
    ('cost_avoided_replicas', '5',
     'Read replicas the bot replaces — without /sql, each team would likely demand its own read replica for ad-hoc analytics. Default 5 = one per pilot team.'),
    ('cost_per_replica_monthly_usd', '200',
     'Monthly all-in cost of one avoided read replica (compute + storage + cross-AZ traffic). Sized for a small db.t3/m6 class with moderate storage. Bump for larger instances.'),
    ('cost_other_monthly_usd', '0',
     'Free-form additional monthly saving — bastion host removal, BI seat avoidance, custom portal dev-time amortized, etc. Set if you want to credit those line items.')
ON CONFLICT (key) DO NOTHING;


-- ---------- 2. Helper function for numeric config lookup ----------

CREATE OR REPLACE FUNCTION p_metrics_cfg_num(p_key text) RETURNS numeric
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(NULLIF(value, '')::numeric, 0)
      FROM bot_config WHERE key = p_key;
$$;
COMMENT ON FUNCTION p_metrics_cfg_num(text) IS
$$Read a numeric value from bot_config. Returns 0 for missing / empty / non-numeric rows.$$;


-- ---------- 3. Re-namespace rating views from v_* to p_metrics_* ----------
-- Idempotent: works whether the old v_* views still exist (first run)
-- or have already been renamed/dropped (re-run). DROP IF EXISTS the
-- old names, then CREATE OR REPLACE the new names below.

DROP VIEW IF EXISTS v_rating_weekly;
DROP VIEW IF EXISTS v_rating_response_rate;
DROP VIEW IF EXISTS v_rating_low_with_feedback;

CREATE OR REPLACE VIEW p_metrics_rating_weekly AS
SELECT date_trunc('week', rated_at)::date AS week,
       count(*)                            AS n_ratings,
       round(avg(rating)::numeric, 2)      AS avg_rating,
       count(*) FILTER (WHERE rating <= 2) AS low_count,
       count(*) FILTER (WHERE rating >= 4) AS high_count,
       count(feedback_text)                AS with_feedback
  FROM request_ratings
 GROUP BY 1
 ORDER BY 1 DESC;
COMMENT ON VIEW p_metrics_rating_weekly IS
$$Weekly rollup of request_ratings. Columns: week (Monday), n_ratings, avg_rating, low_count (≤2), high_count (≥4), with_feedback.$$;

CREATE OR REPLACE VIEW p_metrics_rating_response_rate AS
SELECT date_trunc('week', r.completed_at)::date  AS week,
       count(*)                                  AS terminal_requests,
       count(rr.*)                               AS rated,
       round((count(rr.*)::numeric * 100)
             / NULLIF(count(*), 0), 1)            AS response_pct
  FROM requests r
  LEFT JOIN request_ratings rr ON rr.request_id = r.id
 WHERE r.status IN ('completed','failed','rejected','cancelled')
   AND r.completed_at IS NOT NULL
 GROUP BY 1
 ORDER BY 1 DESC;
COMMENT ON VIEW p_metrics_rating_response_rate IS
$$Weekly response rate: of all requests reaching a terminal state, what fraction received a user rating.$$;

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
  FROM request_ratings rr
  JOIN requests r ON r.id = rr.request_id
 WHERE rr.rating <= 2
 ORDER BY rr.rated_at DESC;
COMMENT ON VIEW p_metrics_rating_low_with_feedback IS
$$Drill-down for low ratings (1-2). Includes the request preview so a maintainer can spot patterns (which targets / query shapes correlate with bad UX).$$;


-- ---------- 4. Cost-savings summary (single row) ----------

CREATE OR REPLACE VIEW p_metrics_cost_savings AS
WITH counts AS (
    SELECT
        count(*) FILTER (WHERE created_at >= date_trunc('month', NOW()))   AS req_this_month,
        count(*) FILTER (WHERE created_at >= date_trunc('week',  NOW()))   AS req_this_week,
        count(*) FILTER (WHERE created_at >= date_trunc('day',   NOW()))   AS req_today,
        count(*)                                                            AS req_lifetime
      FROM requests
), cfg AS (
    SELECT
        p_metrics_cfg_num('cost_dba_minutes_per_request') AS dba_min,
        p_metrics_cfg_num('cost_dba_hourly_usd')          AS dba_hr,
        p_metrics_cfg_num('cost_avoided_replicas')        AS n_replicas,
        p_metrics_cfg_num('cost_per_replica_monthly_usd') AS per_replica,
        p_metrics_cfg_num('cost_other_monthly_usd')       AS other
)
SELECT
    -- Inputs
    c.req_this_month, c.req_this_week, c.req_today, c.req_lifetime,
    cfg.dba_min      AS dba_min_per_request,
    cfg.dba_hr       AS dba_hourly_usd,
    cfg.n_replicas   AS avoided_replicas,
    cfg.per_replica  AS per_replica_monthly_usd,
    cfg.other        AS other_monthly_usd,
    -- DBA time -> $
    round(c.req_this_month * cfg.dba_min / 60.0 * cfg.dba_hr, 2)
                                                         AS dba_saving_this_month_usd,
    round(c.req_lifetime   * cfg.dba_min / 60.0 * cfg.dba_hr, 2)
                                                         AS dba_saving_lifetime_usd,
    -- Replica + other (recurring monthly)
    round(cfg.n_replicas * cfg.per_replica, 2)           AS replica_saving_monthly_usd,
    cfg.other                                            AS other_saving_monthly_usd,
    -- Grand totals (DBA time this month + recurring monthly items)
    round(c.req_this_month * cfg.dba_min / 60.0 * cfg.dba_hr
          + cfg.n_replicas * cfg.per_replica
          + cfg.other, 2)                                AS total_saving_this_month_usd
FROM counts c CROSS JOIN cfg;

COMMENT ON VIEW p_metrics_cost_savings IS
$$Single-row cost-saving summary in USD. DBA savings scale with request volume; replica + other are recurring monthly. Inputs come from bot_config.cost_* — edit those rows to match your org's rates.$$;


-- ---------- 5. Volume time-buckets ----------

CREATE OR REPLACE VIEW p_metrics_volume_daily AS
SELECT date_trunc('day', created_at)::date           AS day,
       count(*)                                      AS submitted,
       count(*) FILTER (WHERE status = 'completed')  AS completed,
       count(*) FILTER (WHERE status = 'rejected')   AS rejected,
       count(*) FILTER (WHERE status = 'failed')     AS failed,
       count(*) FILTER (WHERE status = 'cancelled')  AS cancelled,
       count(DISTINCT requester_slack_id)            AS active_users
  FROM requests
 WHERE created_at >= NOW() - INTERVAL '90 days'
 GROUP BY 1 ORDER BY 1 DESC;
COMMENT ON VIEW p_metrics_volume_daily IS
$$Daily request volume + status breakdown + DAU. Last 90 days.$$;


CREATE OR REPLACE VIEW p_metrics_volume_weekly AS
SELECT date_trunc('week', created_at)::date          AS week,
       count(*)                                      AS submitted,
       count(*) FILTER (WHERE status = 'completed')  AS completed,
       count(*) FILTER (WHERE status = 'rejected')   AS rejected,
       count(*) FILTER (WHERE status = 'failed')     AS failed,
       count(*) FILTER (WHERE status = 'cancelled')  AS cancelled,
       count(DISTINCT requester_slack_id)            AS wau
  FROM requests
 GROUP BY 1 ORDER BY 1 DESC;
COMMENT ON VIEW p_metrics_volume_weekly IS
$$Weekly request volume + status + WAU. All-time.$$;


CREATE OR REPLACE VIEW p_metrics_volume_monthly AS
SELECT date_trunc('month', created_at)::date         AS month,
       count(*)                                      AS submitted,
       count(*) FILTER (WHERE status = 'completed')  AS completed,
       count(*) FILTER (WHERE status = 'rejected')   AS rejected,
       count(*) FILTER (WHERE status = 'failed')     AS failed,
       count(*) FILTER (WHERE status = 'cancelled')  AS cancelled,
       count(DISTINCT requester_slack_id)            AS mau
  FROM requests
 GROUP BY 1 ORDER BY 1 DESC;
COMMENT ON VIEW p_metrics_volume_monthly IS
$$Monthly request volume + status + MAU. All-time.$$;


-- ---------- 6. Team breakdown ----------

CREATE OR REPLACE VIEW p_metrics_team_usage AS
SELECT t.name                                            AS team,
       count(DISTINCT r.requester_slack_id)              AS active_users,
       count(*)                                          AS total_requests,
       count(*) FILTER (WHERE r.status = 'completed')    AS completed,
       count(*) FILTER (WHERE r.status = 'rejected')     AS rejected,
       count(*) FILTER (WHERE r.status = 'failed')       AS failed,
       round(avg(EXTRACT(EPOCH FROM (r.completed_at - r.executed_at)))::numeric, 1)
                                                          AS avg_exec_seconds,
       max(r.created_at)                                  AS last_request_at
  FROM requests r
  JOIN team_members tm ON tm.slack_user_id = r.requester_slack_id
  JOIN teams t         ON t.id = tm.team_id
 GROUP BY t.name
 ORDER BY total_requests DESC;
COMMENT ON VIEW p_metrics_team_usage IS
$$Per-team usage. A user in multiple teams contributes to each — totals may exceed sum of requests.$$;


-- ---------- 7. Top users (leaderboard) ----------

CREATE OR REPLACE VIEW p_metrics_top_users AS
SELECT r.requester_slack_id,
       COALESCE(rq.name, r.requester_name, '(?)')                       AS name,
       rq.email,
       count(*)                                                         AS total_requests,
       count(*) FILTER (WHERE r.created_at >= NOW() - INTERVAL '30 days') AS last_30d,
       count(*) FILTER (WHERE r.created_at >= NOW() - INTERVAL '7 days')  AS last_7d,
       count(*) FILTER (WHERE r.status = 'completed')                   AS completed,
       count(*) FILTER (WHERE r.status = 'rejected')                    AS rejected,
       max(r.created_at)                                                AS last_request_at
  FROM requests r
  LEFT JOIN requesters rq ON rq.slack_user_id = r.requester_slack_id
 GROUP BY r.requester_slack_id, rq.name, r.requester_name, rq.email
 ORDER BY total_requests DESC;
COMMENT ON VIEW p_metrics_top_users IS
$$Per-user request volume. ORDER BY already ranks; LIMIT 10 for top-N.$$;


-- ---------- 8. Scheduled feature usage ----------

CREATE OR REPLACE VIEW p_metrics_scheduled_usage AS
SELECT date_trunc('week', created_at)::date                          AS week,
       count(*)                                                      AS total_requests,
       count(*) FILTER (WHERE scheduled_for IS NOT NULL)             AS scheduled,
       round(100.0 * count(*) FILTER (WHERE scheduled_for IS NOT NULL)
             / NULLIF(count(*), 0), 1)                               AS scheduled_pct,
       count(*) FILTER (WHERE scheduled_for IS NOT NULL
                          AND status = 'completed')                  AS scheduled_completed,
       count(*) FILTER (WHERE scheduled_for IS NOT NULL
                          AND status = 'cancelled')                  AS scheduled_cancelled
  FROM requests
 WHERE created_at >= NOW() - INTERVAL '90 days'
 GROUP BY 1 ORDER BY 1 DESC;
COMMENT ON VIEW p_metrics_scheduled_usage IS
$$Scheduled-query adoption (weekly): how many requests deferred, success/cancel rate.$$;


-- ---------- 9. Tier distribution (ro / rw / ddl) ----------

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
      FROM requests
)
SELECT week,
       count(*) FILTER (WHERE tier = 'ro')           AS ro,
       count(*) FILTER (WHERE tier = 'rw')           AS rw,
       count(*) FILTER (WHERE tier = 'ddl_or_other') AS ddl_or_other,
       count(*)                                      AS total
  FROM classified
 GROUP BY week
 ORDER BY week DESC;
COMMENT ON VIEW p_metrics_tier_distribution IS
$$Weekly tier mix derived from leading SQL keyword. CTE-DML and SET preludes classified as their leading word (cheap approximation, ~99% accurate).$$;


-- ---------- 10. Failure breakdown ----------

CREATE OR REPLACE VIEW p_metrics_failure_breakdown AS
SELECT date_trunc('week', completed_at)::date         AS week,
       count(*) FILTER (WHERE status = 'completed')   AS completed,
       count(*) FILTER (WHERE status = 'rejected')    AS admin_rejected,
       count(*) FILTER (WHERE status = 'failed')      AS execute_failed,
       count(*) FILTER (WHERE status = 'cancelled')   AS user_cancelled,
       count(*)                                       AS terminal_total,
       round(100.0 * count(*) FILTER (WHERE status = 'completed')
             / NULLIF(count(*), 0), 1)                AS success_pct
  FROM requests
 WHERE completed_at IS NOT NULL
 GROUP BY 1 ORDER BY 1 DESC;
COMMENT ON VIEW p_metrics_failure_breakdown IS
$$Per-week outcomes for terminal requests + success rate.$$;


-- ---------- 11. Admin workload ----------

CREATE OR REPLACE VIEW p_metrics_admin_workload AS
WITH actions AS (
    SELECT actor_slack_id, actor_name, action, created_at
      FROM audit_log
     WHERE action IN ('approved', 'rejected', 'changes_requested')
       AND actor_slack_id IS NOT NULL
)
SELECT actor_slack_id,
       MAX(actor_name)                                                   AS name,
       count(*)                                                          AS total_actions,
       count(*) FILTER (WHERE action = 'approved')                       AS approved,
       count(*) FILTER (WHERE action = 'rejected')                       AS rejected,
       count(*) FILTER (WHERE action = 'changes_requested')              AS changes_requested,
       count(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')   AS last_7d,
       count(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days')  AS last_30d,
       max(created_at)                                                   AS last_action_at
  FROM actions
 GROUP BY actor_slack_id
 ORDER BY total_actions DESC;
COMMENT ON VIEW p_metrics_admin_workload IS
$$Per-admin approval volume across approve/reject/changes-requested. Surfaces workload imbalance and admin responsiveness.$$;
