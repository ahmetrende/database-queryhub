-- `/sql whoami` and `/sql roles` tell a person which teams they are in.
--
-- The view behind them read `teams` / `team_members`, which the pod cutover
-- emptied: the teams column came back NULL for all 29 people. Everyone was
-- told they belong to no team -- and since a pod grant IS a team grant, they
-- also could not see where their access comes from.
--
-- A UNION over both models rather than a branch on the switch. A view cannot
-- read `bot_config` without a function call per row, and the union needs no
-- switch to be correct: whichever side is empty contributes nothing, and
-- during a migration both are simply true at once. DISTINCT because a team
-- carried by the mirror appears on both sides under the same name.
--
-- The new side prefers `display_name`: a pod's code is what the importer
-- writes and its label is what people call it, and this string is read by a
-- human in Slack.

CREATE OR REPLACE VIEW p_metrics_who_can_what AS
 WITH admin_info AS (
         SELECT admins.slack_user_id,
            admins.name,
            admins.email,
            admins.max_tier,
            admins.scope_team_ids,
            admins.scope_target_ids
           FROM admins
          WHERE admins.enabled = true
        ), requester_info AS (
         SELECT requesters.slack_user_id,
            requesters.name,
            requesters.email,
            requesters.bypass_team_grants AS bypass
           FROM requesters
          WHERE requesters.enabled = true
        ), teams_per_user AS (
         SELECT s.slack_user_id,
            array_agg(DISTINCT s.team_name ORDER BY s.team_name) AS teams
           FROM (
                SELECT tm.slack_user_id, t_1.name AS team_name
                  FROM team_members tm
                  JOIN teams t_1 ON t_1.id = tm.team_id
              UNION
                SELECT i.external_id AS slack_user_id,
                       COALESCE(t_2.display_name, t_2.name) AS team_name
                  FROM team_member m
                  JOIN team t_2 ON t_2.id = m.team_id AND NOT t_2.is_deleted
                  JOIN principal_identity i
                    ON i.principal_id = m.principal_id
                   AND i.provider = 'slack' AND NOT i.is_deleted
                 WHERE NOT m.is_deleted
           ) s
          GROUP BY s.slack_user_id
        ), user_grants_per_user AS (
         SELECT ug_1.slack_user_id,
            array_agg(((ts.alias || '('::text) || ug_1.mode) || ')'::text ORDER BY ts.alias) AS user_grants
           FROM user_target_grants ug_1
             JOIN target_servers ts ON ts.id = ug_1.target_server_id
          GROUP BY ug_1.slack_user_id
        ), all_users AS (
         SELECT admin_info.slack_user_id
           FROM admin_info
        UNION
         SELECT requester_info.slack_user_id
           FROM requester_info
        )
 SELECT u.slack_user_id,
    COALESCE(a.name, r.name, '(?)'::text) AS name,
    COALESCE(a.email, r.email) AS email,
    a.slack_user_id IS NOT NULL AS is_admin,
    a.max_tier AS admin_max_tier,
    a.scope_team_ids AS admin_scope_team_ids,
    a.scope_target_ids AS admin_scope_target_ids,
    COALESCE(r.bypass, false) AS is_bypass,
    t.teams,
    ug.user_grants
   FROM all_users u
     LEFT JOIN admin_info a ON a.slack_user_id = u.slack_user_id
     LEFT JOIN requester_info r ON r.slack_user_id = u.slack_user_id
     LEFT JOIN teams_per_user t ON t.slack_user_id = u.slack_user_id
     LEFT JOIN user_grants_per_user ug ON ug.slack_user_id = u.slack_user_id
  ORDER BY (a.slack_user_id IS NOT NULL) DESC, (COALESCE(r.bypass, false)) DESC, (COALESCE(a.name, r.name, '(?)'::text));
