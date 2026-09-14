-- =============================================================================
-- QueryHub — DDL role bootstrap on a TARGET Postgres server.
--
-- WHAT THIS DOES
--   Creates (or updates) a `dba_slackbot_ddl` login role and sets its
--   password / connection limit / CONNECT. It does NOT pick a DDL
--   privilege model for you — that choice is cluster-specific and you
--   must fill in section 5 below before the role can run CREATE / ALTER
--   / DROP. After provisioning, register the ddl credential on the
--   target row (deploy/db_admin_templates.sql, username_ddl /
--   password_ddl_encrypted).
--
-- WHY THE DDL GRANTS ARE LEFT TO YOU
--   DDL scope is a judgement call per cluster and there is no single
--   safe default. Object ownership matters: even broad privileges won't
--   let the role ALTER / DROP an object owned by someone else — those
--   land in the bot's `awaiting_dba_manual` flow for a human to finish.
--   Pick the model that matches the cluster's ownership layout.
--
-- HOW TO RUN
--   read -s -p 'New ddl password: ' DDL_PASS; echo
--   PGPASSWORD=<admin> psql -h <host> -U <admin-user> -d <each-db> \
--       -v ddl_password="$DDL_PASS" -f deploy/grant_ddl.sql
--   unset DDL_PASS PGPASSWORD
--
-- IDEMPOTENCY
--   Re-runnable. Password is reset on each run (doubles as rotation).
-- =============================================================================

\if :{?ddl_password}
\else
  \echo
  \echo 'ERROR: pass -v ddl_password=... on the psql command line.'
  \echo '       See the header of this file for the recommended invocation.'
  \quit
\endif

\set ON_ERROR_STOP on

-- 1. Role: create if missing.
SELECT format('CREATE ROLE dba_slackbot_ddl LOGIN PASSWORD %L', :'ddl_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dba_slackbot_ddl')\gexec

-- 2. Always (re-)set the password (rotation path).
SELECT format('ALTER ROLE dba_slackbot_ddl WITH LOGIN PASSWORD %L', :'ddl_password')\gexec

-- 3. Connection limit — DDL traffic is rare, keep it tight.
ALTER ROLE dba_slackbot_ddl CONNECTION LIMIT 3;

-- 4. CONNECT on the current database.
SELECT format('GRANT CONNECT ON DATABASE %I TO dba_slackbot_ddl', current_database())\gexec

-- 5. DDL PRIVILEGE MODEL — CHOOSE ONE, UNCOMMENT, AND ADJUST.
--    All options are commented out by design; the role can't do DDL
--    until you enable one. See the header for why this is left to you.
--
--    Option A — read/write + CREATE in each schema (most conservative).
--      Covers CREATE TABLE/INDEX and ALTER/DROP of objects this role
--      owns. Cannot touch objects owned by other roles → those go to
--      awaiting_dba_manual.
--        GRANT pg_read_all_data  TO dba_slackbot_ddl;
--        GRANT pg_write_all_data TO dba_slackbot_ddl;
--        DO $do$
--        DECLARE s text;
--        BEGIN
--          FOR s IN SELECT nspname FROM pg_namespace
--                    WHERE nspname NOT IN ('pg_catalog','information_schema','pg_toast')
--                      AND nspname NOT LIKE 'pg\_temp\_%'
--                      AND nspname NOT LIKE 'pg\_toast\_%'
--          LOOP
--            EXECUTE format('GRANT USAGE, CREATE ON SCHEMA %I TO dba_slackbot_ddl', s);
--          END LOOP;
--        END $do$;
--
--    Option B — take ownership of a schema's objects (lets the role
--      ALTER/DROP them). Run REASSIGN OWNED / per-object ALTER ... OWNER
--      to dba_slackbot_ddl for the schemas it should fully manage.
--      Cluster-specific; write the exact statements here.
--
--    Option C — rds_superuser membership (broadest; use with care).
--      Gives the role near-unrestricted DDL on RDS. Only if a narrower
--      model is impractical for this cluster.
--        GRANT rds_superuser TO dba_slackbot_ddl;

\echo
\echo '== dba_slackbot_ddl role created/updated in:'
SELECT current_database() AS database;

\echo
\echo '== role memberships (verify your chosen model from section 5):'
SELECT r.rolname AS member_of
FROM pg_auth_members m
JOIN pg_roles r ON r.oid = m.roleid
WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = 'dba_slackbot_ddl')
ORDER BY r.rolname;

\echo
\echo '== role attribute summary:'
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolconnlimit
FROM pg_roles WHERE rolname = 'dba_slackbot_ddl';

\echo
\echo 'NOTE: if section 5 is still commented out, the role can log in but'
\echo 'cannot run DDL yet. Choose a privilege model before relying on it.'
\echo
\echo 'Then register the ddl credential in the bot DB:'
\echo '  1. python scripts/encrypt_secret.py    (encrypt the ddl password)'
\echo '  2. UPDATE target_servers SET username_ddl=..., password_ddl_encrypted=...'
