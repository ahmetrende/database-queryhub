-- =============================================================================
-- QueryHub — team admin templates (IDE-agnostic, psql variable style)
-- =============================================================================
-- Each block is a self-contained snippet. The `:'name'` syntax works in:
--
--   - psql (CLI)            : pass values with -v name=value
--   - DataGrip / DataSpell  : highlight block + run, dialog prompts for params
--   - DBeaver / IntelliJ    : same as DataGrip (JDBC named-param binding)
--
-- For psql one-liners:
--
--   psql -h <bot-db-host> -U slackbot -d slackbot \
--        -v team_name=payment \
--        -v slack_user_id=U0PAYMENT01 \
--        -v target_server_id=5 \
--        -v dbs_csv='payment_db,orders_db' \
--        -f deploy/team_admin_templates.sql
--
-- For DataGrip/etc, just highlight one block and execute — dialog asks
-- for every :'param' in the block.
--
-- ARRAY param trick: psql + most IDEs don't have a native array param. We
-- pass a comma-separated string in :'dbs_csv', then string_to_array() it.
-- Pass empty string '' for "all databases on this target".
-- =============================================================================


-- ---------- 1. Create a team ----------
INSERT INTO teams (name, description)
VALUES (:'team_name', :'team_description')
RETURNING id, name, description;


-- ---------- 2. Add a member to a team ----------
INSERT INTO team_members (team_id, slack_user_id)
VALUES (
    (SELECT id FROM teams WHERE name = :'team_name'),
    :'slack_user_id'
)
RETURNING team_id, slack_user_id, added_at;


-- ---------- 3. Grant a team access to a target ----------
-- :dbs_csv = ''  → all databases allowed on this target (NULL stored)
-- :dbs_csv = 'db1,db2,db3' → only those databases
-- :mode = 'ro' (default) | 'rw' | 'ddl'
INSERT INTO team_target_grants (team_id, target_server_id, allowed_databases, mode)
VALUES (
    (SELECT id FROM teams WHERE name = :'team_name'),
    :'target_server_id'::int,
    CASE
        WHEN :'dbs_csv' = '' THEN NULL
        ELSE string_to_array(:'dbs_csv', ',')
    END,
    :'mode'
)
RETURNING team_id, target_server_id, allowed_databases, mode;


-- ---------- 3b. Update an existing team grant's mode ----------
UPDATE team_target_grants
SET mode = :'mode'
WHERE team_id = (SELECT id FROM teams WHERE name = :'team_name')
  AND target_server_id = :'target_server_id'::int
RETURNING team_id, target_server_id, mode;


-- ---------- 4. Update the allowed-databases list for an existing grant ----------
UPDATE team_target_grants
SET allowed_databases = CASE
        WHEN :'dbs_csv' = '' THEN NULL
        ELSE string_to_array(:'dbs_csv', ',')
    END
WHERE team_id = (SELECT id FROM teams WHERE name = :'team_name')
  AND target_server_id = :'target_server_id'::int
RETURNING team_id, target_server_id, allowed_databases;


-- ---------- 4b. Bind a Postgres role on the target to this grant ----------
-- After provisioning the role on the target with deploy/grant_team_role.sql,
-- store its name here so the bot does `SET LOCAL ROLE <role>` before each
-- query from this team. Pass empty string to clear (run as bot login user).
UPDATE team_target_grants
SET target_role = NULLIF(:'target_role', '')
WHERE team_id = (SELECT id FROM teams WHERE name = :'team_name')
  AND target_server_id = :'target_server_id'::int
RETURNING team_id, target_server_id, target_role;


-- ---------- 5. Remove a member from a team ----------
DELETE FROM team_members
WHERE team_id = (SELECT id FROM teams WHERE name = :'team_name')
  AND slack_user_id = :'slack_user_id'
RETURNING team_id, slack_user_id;


-- ---------- 6. Revoke a team's access to a target ----------
DELETE FROM team_target_grants
WHERE team_id = (SELECT id FROM teams WHERE name = :'team_name')
  AND target_server_id = :'target_server_id'::int
RETURNING team_id, target_server_id;


-- ---------- 7. Delete an entire team (members + grants cascade automatically) ----------
DELETE FROM teams
WHERE name = :'team_name'
RETURNING id, name;


-- =============================================================================
-- USER-LEVEL OVERRIDES (user_target_grants)
-- =============================================================================
-- A row here for (slack_user_id, target_server_id) ENTIRELY supersedes any
-- team grants the user has on that target. Use when:
--   - giving a user access to a target their team doesn't have
--   - restricting a user to ro on a target where their team is rw
--   - elevating a single user to ddl without elevating the whole team
-- (See v_effective_user_grants for the resolved per-user picture.)


-- ---------- 8. Grant a user direct access (overrides team grants) ----------
INSERT INTO user_target_grants
    (slack_user_id, target_server_id, allowed_databases, mode, granted_by)
VALUES (
    :'slack_user_id',
    :'target_server_id'::int,
    CASE
        WHEN :'dbs_csv' = '' THEN NULL
        ELSE string_to_array(:'dbs_csv', ',')
    END,
    :'mode',
    :'granted_by_slack_id'
)
ON CONFLICT (slack_user_id, target_server_id) DO UPDATE
    SET allowed_databases = EXCLUDED.allowed_databases,
        mode              = EXCLUDED.mode,
        granted_at        = NOW(),
        granted_by        = EXCLUDED.granted_by
RETURNING slack_user_id, target_server_id, allowed_databases, mode;


-- ---------- 9. Drop a user-level override (falls back to team grants) ----------
DELETE FROM user_target_grants
WHERE slack_user_id = :'slack_user_id'
  AND target_server_id = :'target_server_id'::int
RETURNING slack_user_id, target_server_id;


-- ---------- 10. Toggle cross-team bypass for a user ----------
-- TRUE  = user sees every enabled target (admin-like visibility)
-- FALSE = user respects team_target_grants (default)
UPDATE requesters
SET bypass_team_grants = :'bypass'::boolean
WHERE slack_user_id = :'slack_user_id'
RETURNING slack_user_id, name, bypass_team_grants;


-- =============================================================================
-- Inspection queries
-- =============================================================================


-- All teams + member/grant counts (no params)
SELECT * FROM v_team_summary;


-- One team's members + grants (uses :'team_name')
SELECT t.id          AS team_id,
       t.name        AS team_name,
       t.description,
       tm.slack_user_id,
       tm.added_at
FROM teams t
LEFT JOIN team_members tm ON tm.team_id = t.id
WHERE t.name = :'team_name'
ORDER BY tm.slack_user_id;

SELECT t.name        AS team_name,
       g.target_server_id,
       ts.alias      AS target_alias,
       ts.host,
       g.allowed_databases,
       g.granted_at
FROM teams t
LEFT JOIN team_target_grants g ON g.team_id = t.id
LEFT JOIN target_servers ts    ON ts.id = g.target_server_id
WHERE t.name = :'team_name'
ORDER BY ts.alias;


-- What targets/databases is a given Slack user allowed to reach (legacy view, team-only)?
SELECT * FROM v_user_targets WHERE slack_user_id = :'slack_user_id';


-- Effective grant per (user, target) including user-level overrides + mode tier
SELECT * FROM v_effective_user_grants WHERE slack_user_id = :'slack_user_id'
ORDER BY target_server_id;


-- Reverse direction: who can reach a given target?
SELECT slack_user_id, target_alias, allowed_databases
FROM v_user_targets
WHERE target_id = :'target_server_id'::int
ORDER BY slack_user_id;
