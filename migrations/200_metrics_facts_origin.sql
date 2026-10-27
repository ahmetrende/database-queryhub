-- 200: the metrics facts view says which channel each request came through.
--
-- `requests.origin` already names the door (slack, web, idp, mcp), but the
-- facts view behind the metrics dashboard did not carry it, so no panel could
-- show the channel mix. The IdP portal opened to the whole company on
-- 2026-10-27 and the web UI is now limited to a few people, so the mix changes
-- fast and the dashboard needs to show it.
--
-- This migration adds one column at the end, so every reader of the view
-- keeps working. The column is `origin`. It is lower-cased and trimmed. A blank
-- value reads as 'slack', the default of `requests.origin` and of
-- origins.normalize(). The rest of the view is migration 135, unchanged.

CREATE OR REPLACE VIEW p_metrics_request_facts AS
WITH classified AS (
    SELECT r.*,
           CASE COALESCE(q.executed_tier::text, q.required_tier::text)
                WHEN 'ro'  THEN 'ro'
                WHEN 'rw'  THEN 'rw'
                WHEN 'ddl' THEN 'ddl_or_other'
                ELSE CASE upper(substring(
                         regexp_replace(r.query,
                             '^(\s+|--[^\n]*(\n|$)|/\*([^*]|\*+[^*/])*\*+/)*', '')
                         FROM '^[A-Za-z]+'))
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
                     END
           END AS tier
      FROM requests_reportable r
      JOIN requests q ON q.id = r.id
), pod AS (
    SELECT i.external_id AS slack_user_id,
           min(COALESCE(t.display_name, t.name)) AS team
      FROM team_member m
      JOIN team t ON t.id = m.team_id AND NOT t.is_deleted
      JOIN principal_identity i ON i.principal_id = m.principal_id
                               AND i.provider = 'slack' AND NOT i.is_deleted
     WHERE NOT m.is_deleted
     GROUP BY i.external_id
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
       pod.team                                                      AS team,
       c.target_server_id                                            AS target_id,
       ts.alias                                                      AS target_alias,
       c.database_name,
       c.tier,
       c.decided_by_slack_id,
       CASE WHEN c.decided_by_slack_id = 'AUTO' THEN 'auto-approved'
            ELSE COALESCE(
                (SELECT a.name FROM admins a
                  WHERE a.slack_user_id = c.decided_by_slack_id),
                (SELECT p.display_name
                   FROM principal_identity i
                   JOIN principal p ON p.id = i.principal_id
                  WHERE i.provider = 'slack' AND NOT i.is_deleted
                    AND i.external_id = c.decided_by_slack_id
                  ORDER BY p.id LIMIT 1),
                c.decided_by_name)
       END                                                           AS decided_by_name,
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
         WHERE rr.request_id = c.id LIMIT 1)                         AS rating,
       (c.decided_by_slack_id = 'AUTO')                              AS auto_approved,
       COALESCE(NULLIF(lower(btrim(c.origin)), ''), 'slack')         AS origin
  FROM classified c
  LEFT JOIN target_servers ts ON ts.id = c.target_server_id
  LEFT JOIN pod ON pod.slack_user_id = c.requester_slack_id
 WHERE c.created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date;

COMMENT ON VIEW p_metrics_request_facts IS
$$One row per reportable request, denormalized for client-side filtering
and aggregation in the metrics dashboard. team = the requester's current pod
(alphabetically first); tier = the tier the request ran at, else the first
keyword after comments; auto_approved = approved by a grant, not a person;
origin = the channel the request came through (slack, web, idp,
mcp; blank reads as slack, as the bot's own default does);
report_start_date trims the early dev window.$$;
