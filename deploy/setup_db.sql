-- Bootstrap the bot's metadata database.
--
-- Run ONCE as a Postgres superuser (the RDS master user) BEFORE applying the
-- migrations in migrations/. After this, the bot connects as `slackbot` with
-- the password you pass via -v.
--
-- Usage (recommended — keeps password out of shell history and git):
--
--   read -s -p 'New slackbot password: ' BOT_DB_PASSWORD; echo
--   read -s -p 'RDS admin (e.g. postgres) password: ' PGPASSWORD; echo; export PGPASSWORD
--   psql -h <dba-rds-host> -U <admin-user> -d postgres \
--        -v bot_password="$BOT_DB_PASSWORD" \
--        -f deploy/setup_db.sql
--   unset PGPASSWORD BOT_DB_PASSWORD
--
-- Idempotent: safe to re-run (will reset the slackbot password to the new value).

\if :{?bot_password}
\else
  \echo
  \echo 'ERROR: pass -v bot_password=... on the psql command line.'
  \echo '       See the header of this file for the recommended invocation.'
  \quit
\endif

-- Stop on any error so partial state is impossible.
\set ON_ERROR_STOP on

-- 1. Role: create if missing.
SELECT format('CREATE ROLE slackbot LOGIN PASSWORD %L', :'bot_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'slackbot')\gexec

-- 2. Always (re-)set the password on the role. Lets you re-run with a new
--    password to rotate it.
SELECT format('ALTER ROLE slackbot WITH LOGIN PASSWORD %L', :'bot_password')\gexec

-- 3. Connection limit. The role is already non-privileged because CREATE ROLE
--    defaults to NOSUPERUSER / NOCREATEDB / NOCREATEROLE / NOREPLICATION, so
--    we don't ALTER those bits (RDS rds_superuser cannot toggle the SUPERUSER
--    or REPLICATION attributes anyway).
ALTER ROLE slackbot CONNECTION LIMIT 20;

-- 4. Grant slackbot membership to the current admin so we can (a) create the
--    database and (b) transfer ownership to slackbot. RDS's rds_superuser
--    cannot create a DB owned by another role without being a member of it.
DO $$
BEGIN
    EXECUTE format('GRANT slackbot TO %I', current_user);
END$$;

-- 5. Create the database if missing (initially owned by current admin).
SELECT 'CREATE DATABASE slackbot ENCODING UTF8'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'slackbot')\gexec

-- 6. Transfer ownership to slackbot so migrations can create schema objects
--    without extra grants.
ALTER DATABASE slackbot OWNER TO slackbot;

\echo
\echo 'Done.'
\echo 'Verify:'
\echo '  PGPASSWORD=$BOT_DB_PASSWORD psql -h <host> -U slackbot -d slackbot -c ''select current_user, current_database();'''
\echo
\echo 'Then fill /etc/slackbot/env and run migrations:'
\echo '  cd <repo-path>'
\echo '  set -a; source /etc/slackbot/env; set +a'
\echo '  .venv/bin/python scripts/apply_migrations.py'
\echo
