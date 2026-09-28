-- 135: the metrics views read the access model the fleet runs on.
--
-- Pods replaced teams on 2026-09-08 and the legacy `teams` / `team_members`
-- tables were emptied, but three views still read them. Every request in the
-- dashboard came out "(unteamed)" (0 of 6,933 had a team), and the who-can-what
-- table behind `/sql whoami`, `/sql roles` and both dashboards listed legacy
-- grants and missed every role and grant written since.
--
-- Fixed here, with the column list of each view unchanged:
--
-- p_metrics_request_facts
--   team             the requester's current pod (team_member, via their Slack
--                    identity), alphabetically first. 6,843 of 6,933 rows get one.
--   tier             the tier the request was classified and ran at
--                    (executed_tier, then required_tier). Older rows fall back to
--                    the first keyword, now after leading comments and
--                    whitespace: "-- note\nSELECT" and "SELECT\n*" were counted
--                    as ddl (477 read-only requests).
--   decided_by_name  the approver's name also when they are not in `admins`:
--                    a pod captain's decisions showed as a bare Slack id, because
--                    the fallback sat inside a subquery that returned no row.
--                    Auto-approvals read "auto-approved".
--   auto_approved    new, last column: the request was approved by a grant, not
--                    a person, so latency and workload charts can leave it out.
-- p_metrics_team_usage   the same per-pod counts, from the facts view.
-- p_metrics_who_can_what  roles (admin, approver) from role_assignment and
--                    direct grants from access_grant; teams from team_member.

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
       (c.decided_by_slack_id = 'AUTO')                              AS auto_approved
  FROM classified c
  LEFT JOIN target_servers ts ON ts.id = c.target_server_id
  LEFT JOIN pod ON pod.slack_user_id = c.requester_slack_id
 WHERE c.created_at >= p_metrics_cfg_text('report_start_date', '2026-05-01')::date;

COMMENT ON VIEW p_metrics_request_facts IS
$$One row per reportable request, denormalized for client-side filtering
and aggregation in the metrics dashboard. team = the requester's current pod
(alphabetically first); tier = the tier the request ran at, else the first
keyword after comments; auto_approved = approved by a grant, not a person;
report_start_date trims the early dev window.$$;


CREATE OR REPLACE VIEW p_metrics_team_usage AS
SELECT f.team,
       count(DISTINCT f.requester_slack_id)                  AS active_users,
       count(*)                                              AS total_requests,
       count(*) FILTER (WHERE f.status = 'completed')        AS completed,
       count(*) FILTER (WHERE f.status = 'rejected')         AS rejected,
       count(*) FILTER (WHERE f.status = 'failed')           AS failed,
       round(avg(f.exec_sec), 1)                             AS avg_exec_seconds,
       max(f.created_at)                                     AS last_request_at
  FROM p_metrics_request_facts f
 WHERE f.team IS NOT NULL
 GROUP BY f.team
 ORDER BY count(*) DESC;


CREATE OR REPLACE VIEW p_metrics_who_can_what AS
WITH person AS (
    -- One row per Slack identity; an identity recorded twice counts once.
    SELECT DISTINCT ON (i.external_id)
           i.external_id AS slack_user_id, p.id AS principal_id,
           p.display_name, p.email
      FROM principal p
      JOIN principal_identity i ON i.principal_id = p.id
                               AND i.provider = 'slack' AND NOT i.is_deleted
     WHERE p.kind = 'person' AND p.enabled AND NOT p.is_deleted
     ORDER BY i.external_id, p.id
), live_role AS (
    SELECT ra.*
      FROM role_assignment ra
     WHERE ra.role IN ('admin', 'approver')
       AND NOT ra.is_deleted AND ra.revoked_at IS NULL
       AND (ra.valid_from IS NULL OR ra.valid_from <= now())
       AND (ra.valid_until IS NULL OR ra.valid_until > now())
), admin_info AS (
    SELECT principal_id,
           CASE WHEN bool_or(any_tier) THEN NULL
                ELSE (array_agg(max_tier ORDER BY CASE max_tier
                          WHEN 'ddl' THEN 3 WHEN 'rw' THEN 2 ELSE 1 END DESC))[1]
           END::text                                          AS max_tier,
           CASE WHEN bool_or(all_teams) THEN NULL
                ELSE (array_agg(DISTINCT scope_team_id)
                        FILTER (WHERE scope_team_id IS NOT NULL))::int[]
           END                                                AS scope_team_ids,
           CASE WHEN bool_or(all_targets) THEN NULL
                ELSE (array_agg(DISTINCT scope_target_id)
                        FILTER (WHERE scope_target_id IS NOT NULL))::int[]
           END                                                AS scope_target_ids
      FROM live_role
     GROUP BY principal_id
), teams_per AS (
    SELECT m.principal_id,
           array_agg(DISTINCT COALESCE(t.display_name, t.name)
                     ORDER BY COALESCE(t.display_name, t.name)) AS teams
      FROM team_member m
      JOIN team t ON t.id = m.team_id AND NOT t.is_deleted
     WHERE NOT m.is_deleted
     GROUP BY m.principal_id
), grants_per AS (
    SELECT s.principal_id, array_agg(s.grant_text ORDER BY s.grant_text) AS user_grants
      FROM (SELECT DISTINCT g.principal_id,
                   CASE WHEN g.all_targets THEN '*' ELSE COALESCE(ts.alias, '?') END
                   || CASE WHEN g.all_databases OR g.database_name IS NULL THEN ''
                           ELSE '/' || g.database_name END
                   || '(' || g.tier::text || ')'           AS grant_text
              FROM access_grant g
              LEFT JOIN target_servers ts ON ts.id = g.target_id
             WHERE g.principal_id IS NOT NULL
               AND NOT g.is_deleted AND g.revoked_at IS NULL
               AND (g.valid_from IS NULL OR g.valid_from <= now())
               AND (g.valid_until IS NULL OR g.valid_until > now())) s
     GROUP BY s.principal_id
)
SELECT pe.slack_user_id,
       COALESCE(pe.display_name, '(?)')                      AS name,
       pe.email,
       (a.principal_id IS NOT NULL)                          AS is_admin,
       a.max_tier                                            AS admin_max_tier,
       a.scope_team_ids                                      AS admin_scope_team_ids,
       a.scope_target_ids                                    AS admin_scope_target_ids,
       COALESCE(rq.bypass_team_grants, false)                AS is_bypass,
       t.teams,
       gr.user_grants
  FROM person pe
  LEFT JOIN admin_info a ON a.principal_id = pe.principal_id
  LEFT JOIN requesters rq ON rq.slack_user_id = pe.slack_user_id
  LEFT JOIN teams_per t ON t.principal_id = pe.principal_id
  LEFT JOIN grants_per gr ON gr.principal_id = pe.principal_id
 ORDER BY (a.principal_id IS NOT NULL) DESC,
          COALESCE(rq.bypass_team_grants, false) DESC,
          COALESCE(pe.display_name, '(?)');
