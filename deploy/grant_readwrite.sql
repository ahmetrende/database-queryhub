-- =============================================================================
-- QueryHub — read-write role bootstrap on a TARGET Postgres server.
--
-- WHAT THIS DOES
--   Creates (or updates) a `dba_slackbot_rw` role with read + write access
--   (SELECT / INSERT / UPDATE / DELETE) across the database it is run in,
--   via the Postgres 14+ predefined roles pg_read_all_data and
--   pg_write_all_data. No DDL — the role cannot CREATE / ALTER / DROP.
--   After running this, register the rw credential on the target row in
--   the bot DB (deploy/db_admin_templates.sql, the username_rw /
--   password_rw_encrypted UPDATE).
--
-- WHY PREDEFINED ROLES (not per-schema GRANTs like grant_readonly.sql)
--   pg_read_all_data + pg_write_all_data already cover USAGE on every
--   schema plus read/write on every table, view, and sequence — present
--   AND future — so there is no schema loop and no DEFAULT PRIVILEGES to
--   maintain. One membership grant, cluster-wide, that never goes stale.
--
-- WHEN TO RUN
--   - Once per cluster is enough for the data access itself (the
--     membership is cluster-wide), but you still want CONNECT on each
--     database the bot should reach — so run it once per target database,
--     same as grant_readonly.sql.
--
-- HOW TO RUN (manual one-off)
--   read -s -p 'New rw password: ' RW_PASS; echo
--   PGPASSWORD=<admin> psql -h <host> -U <admin-user> -d <each-db> \
--       -v rw_password="$RW_PASS" -f deploy/grant_readwrite.sql
--   unset RW_PASS PGPASSWORD
--
-- IDEMPOTENCY
--   Re-runnable. Existing role's password is reset (doubles as rotation).
--   Membership / CONNECT grants are no-ops if already present.
--
-- RDS NOTES
--   The admin user must have rds_superuser. Granting the predefined
--   pg_read_all_data / pg_write_all_data roles is permitted under
--   rds_superuser.
-- =============================================================================

\if :{?rw_password}
\else
  \echo
  \echo 'ERROR: pass -v rw_password=... on the psql command line.'
  \echo '       See the header of this file for the recommended invocation.'
  \quit
\endif

\set ON_ERROR_STOP on

-- 1. Role: create if missing.
SELECT format('CREATE ROLE dba_slackbot_rw LOGIN PASSWORD %L', :'rw_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dba_slackbot_rw')\gexec

-- 2. Always (re-)set the password (rotation path).
SELECT format('ALTER ROLE dba_slackbot_rw WITH LOGIN PASSWORD %L', :'rw_password')\gexec

-- 3. Connection limit so a runaway bot can never starve other clients.
ALTER ROLE dba_slackbot_rw CONNECTION LIMIT 5;

-- 4. CONNECT on the current database.
SELECT format('GRANT CONNECT ON DATABASE %I TO dba_slackbot_rw', current_database())\gexec

-- 5. Read + write everywhere via the predefined roles (PG 14+).
--    These cover schema USAGE + read/write on existing AND future
--    tables / views / sequences, cluster-wide.
GRANT pg_read_all_data  TO dba_slackbot_rw;
GRANT pg_write_all_data TO dba_slackbot_rw;

-- 6. Sanity check.
\echo
\echo '== dba_slackbot_rw provisioned in:'
SELECT current_database() AS database;

\echo
\echo '== role memberships (expect pg_read_all_data + pg_write_all_data):'
SELECT r.rolname AS member_of
FROM pg_auth_members m
JOIN pg_roles r ON r.oid = m.roleid
WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = 'dba_slackbot_rw')
ORDER BY r.rolname;

\echo
\echo '== role attribute summary:'
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolconnlimit
FROM pg_roles WHERE rolname = 'dba_slackbot_rw';

\echo
\echo 'Done. Register the rw credential in the bot DB next:'
\echo '  1. python scripts/encrypt_secret.py    (encrypt the rw password)'
\echo '  2. UPDATE target_servers SET username_rw=..., password_rw_encrypted=...'
\echo '     See deploy/db_admin_templates.sql or docs/OPERATIONS.md section 5.'
