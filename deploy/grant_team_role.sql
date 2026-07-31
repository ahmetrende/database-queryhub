-- =============================================================================
-- Provision a per-team role on a TARGET Postgres server, so the bot can
-- `SET LOCAL ROLE <role>` for queries originating from that team.
--
-- WHEN TO RUN
--   Once per (team, target cluster). After the role exists at cluster level,
--   you also need to GRANT actual privileges (SELECT/INSERT/etc.) to it on
--   the schemas/tables the team should reach — that part is intentionally
--   left manual because it's domain-specific.
--
-- HOW TO RUN
--   read -s -p 'admin password: ' PGPASSWORD; echo; export PGPASSWORD
--   psql -h <target-host> -U <admin-user> -d <any-db> \
--        -v role_name=slackbot_team_payment \
--        -v bot_login=queryhub_rw \
--        -f deploy/grant_team_role.sql
--   unset PGPASSWORD
--
-- WHAT IT DOES
--   1. Creates `<role_name>` (NOLOGIN) if missing.
--   2. Grants membership of `<role_name>` to `<bot_login>` so the bot can
--      `SET ROLE <role_name>` from its login session.
--   3. Echoes example GRANT statements for you to fill in.
--
-- AFTER THIS
--   - In the bot DB, set team_target_grants.target_role for the team+target
--     pair (see deploy/team_admin_templates.sql).
--   - GRANT specific privileges (SELECT/INSERT/UPDATE/DELETE) to the role
--     on the schemas/tables this team should reach.
-- =============================================================================

\if :{?role_name}
\else
  \echo 'ERROR: pass -v role_name=...'
  \quit
\endif

\if :{?bot_login}
\else
  \echo 'ERROR: pass -v bot_login=...'
  \quit
\endif

\set ON_ERROR_STOP on

-- 1. Create role if missing (NOLOGIN — pure permission container).
SELECT format('CREATE ROLE %I NOLOGIN', :'role_name')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role_name')\gexec

-- 2. Grant membership so SET ROLE from bot's login user works.
SELECT format('GRANT %I TO %I', :'role_name', :'bot_login')\gexec

\echo
\echo 'Role provisioned. Now GRANT actual privileges, e.g.:'
\echo
\echo '  GRANT CONNECT ON DATABASE current_db TO :role_name;'
\echo '  GRANT USAGE ON SCHEMA payment TO :role_name;'
\echo '  GRANT SELECT, INSERT, UPDATE, DELETE'
\echo '        ON ALL TABLES IN SCHEMA payment TO :role_name;'
\echo '  ALTER DEFAULT PRIVILEGES IN SCHEMA payment'
\echo '        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :role_name;'
\echo
\echo 'Then in the BOT DB:'
\echo "  UPDATE team_target_grants SET target_role = '<role_name>'"
\echo '   WHERE team_id = (SELECT id FROM teams WHERE name = ''<team>'')'
\echo '     AND target_server_id = <target_id>;'
\echo
