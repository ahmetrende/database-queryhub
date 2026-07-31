-- p_metrics_request_facts — one denormalized row per reportable request.
--
-- Powers the metrics dashboard's client-side filter + aggregate model:
-- the publisher serialises this view to JSON, the browser slices it on
-- every filter change. Tier is derived from the SQL leading keyword
-- (same heuristic as p_metrics_tier_distribution); team is the
-- alphabetically-first membership for the requester (multi-team users
-- are rare in pilot; UI shows one canonical team).
--
-- Membership in requests_reportable already excludes operator self-test
-- traffic, so this view inherits that filter.

CREATE OR REPLACE VIEW p_metrics_request_facts AS
WITH classified AS (
    SELECT r.*,
           CASE upper(split_part(btrim(r.query), ' ', 1))
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
      FROM requests_reportable r
)
SELECT c.id,
       c.created_at,
       c.decided_at,
       c.executed_at,
       c.completed_at,
       c.scheduled_for,
       c.status::text                                                AS status,
       c.requester_slack_id,
       c.requester_name,
       (SELECT t.name
          FROM teams t
          JOIN team_members tm ON tm.team_id = t.id
         WHERE tm.slack_user_id = c.requester_slack_id
         ORDER BY t.name
         LIMIT 1)                                                    AS team,
       c.target_server_id                                            AS target_id,
       ts.alias                                                      AS target_alias,
       c.database_name,
       c.tier,
       c.decided_by_slack_id,
       (SELECT COALESCE(a.name, c.decided_by_name)
          FROM admins a WHERE a.slack_user_id = c.decided_by_slack_id) AS decided_by_name,
       c.row_count,
       c.truncated,
       c.bundle_id,
       CASE WHEN c.decided_at IS NOT NULL
            THEN round(extract(epoch FROM (c.decided_at - c.created_at))::numeric, 1)
            ELSE NULL END                                            AS approval_sec,
       CASE WHEN c.completed_at IS NOT NULL AND c.executed_at IS NOT NULL
            THEN round(extract(epoch FROM (c.completed_at - c.executed_at))::numeric, 2)
            ELSE NULL END                                            AS exec_sec,
       extract(hour FROM (c.created_at AT TIME ZONE
           p_metrics_cfg_text('report_timezone', 'UTC')))::int       AS hour_local,
       extract(dow  FROM (c.created_at AT TIME ZONE
           p_metrics_cfg_text('report_timezone', 'UTC')))::int       AS dow_local,
       (SELECT rr.rating FROM request_ratings_reportable rr
         WHERE rr.request_id = c.id LIMIT 1)                         AS rating
  FROM classified c
  LEFT JOIN target_servers ts ON ts.id = c.target_server_id
 WHERE c.created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date;

COMMENT ON VIEW p_metrics_request_facts IS
$$One row per reportable request, denormalized for client-side filtering
and aggregation in the metrics dashboard. Tier derived from the SQL
leading keyword; team = alphabetically-first membership for the
requester; report_start_date trims the early dev window.$$;
