-- A single-row-per-user roles + grants overview, used by
-- `/sql roles` (admin-only Slack sub-command) and as the source of
-- truth any DBA can query directly in DataGrip / psql.

CREATE OR REPLACE VIEW p_metrics_who_can_what AS
WITH
admin_info AS (
    SELECT slack_user_id, name, email,
           max_tier, scope_team_ids, scope_target_ids
      FROM admins WHERE enabled = TRUE
),
requester_info AS (
    SELECT slack_user_id, name, email, bypass_team_grants AS bypass
      FROM requesters WHERE enabled = TRUE
),
teams_per_user AS (
    SELECT tm.slack_user_id,
           array_agg(t.name ORDER BY t.name) AS teams
      FROM team_members tm
      JOIN teams t ON t.id = tm.team_id
     GROUP BY tm.slack_user_id
),
user_grants_per_user AS (
    SELECT ug.slack_user_id,
           array_agg(ts.alias || '(' || ug.mode || ')' ORDER BY ts.alias)
               AS user_grants
      FROM user_target_grants ug
      JOIN target_servers ts ON ts.id = ug.target_server_id
     GROUP BY ug.slack_user_id
),
all_users AS (
    SELECT slack_user_id FROM admin_info
    UNION
    SELECT slack_user_id FROM requester_info
)
SELECT u.slack_user_id,
       COALESCE(a.name, r.name, '(?)')                       AS name,
       COALESCE(a.email, r.email)                            AS email,
       (a.slack_user_id IS NOT NULL)                         AS is_admin,
       a.max_tier                                            AS admin_max_tier,
       a.scope_team_ids                                      AS admin_scope_team_ids,
       a.scope_target_ids                                    AS admin_scope_target_ids,
       COALESCE(r.bypass, FALSE)                             AS is_bypass,
       t.teams,
       ug.user_grants
  FROM all_users u
  LEFT JOIN admin_info     a  ON a.slack_user_id  = u.slack_user_id
  LEFT JOIN requester_info r  ON r.slack_user_id  = u.slack_user_id
  LEFT JOIN teams_per_user t  ON t.slack_user_id  = u.slack_user_id
  LEFT JOIN user_grants_per_user ug ON ug.slack_user_id = u.slack_user_id
 ORDER BY (a.slack_user_id IS NOT NULL) DESC,
          COALESCE(r.bypass, FALSE) DESC,
          name NULLS LAST;

COMMENT ON VIEW p_metrics_who_can_what IS
$$One row per active user (admin OR enabled requester). Columns: is_admin (+ admin_max_tier / scope_*), is_bypass, teams[], user_grants[] (target(mode) shorthand). Powers /sql roles and serves as the source of truth for "who can do what".$$;
