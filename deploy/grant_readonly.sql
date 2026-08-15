-- =============================================================================
-- QueryHub — read-only role bootstrap on a TARGET Postgres server.
--
-- WHAT THIS DOES
--   Creates (or updates) a `dba_slackbot_ro` role with read-only access to
--   the database it is run in. After running this in every database that
--   should be queryable via /sql, register the server in `target_servers`
--   (see deploy/db_admin_templates.sql) using dba_slackbot_ro + the
--   password chosen here. The role lacks INSERT / UPDATE / DELETE / DDL —
--   the only damage it could do is run an expensive SELECT.
--
-- WHEN TO RUN
--   - Once per database on a target cluster. Skip template0, template1,
--     rdsadmin (RDS-managed), and any DB you want hidden from the bot.
--   - After CREATE'ing a new database whose tables you want bot-readable
--     (the DEFAULT PRIVILEGES below cover NEW objects in EXISTING schemas,
--     not entirely-new databases).
--
-- HOW TO RUN (manual one-off)
--   read -s -p 'New ro password: ' RO_PASS; echo
--   PGPASSWORD=<admin> psql -h <host> -U <admin-user> -d <each-db> \
--       -v ro_password="$RO_PASS" -f deploy/grant_readonly.sql
--   unset RO_PASS PGPASSWORD
--
-- HOW TO RUN (across many DBs on one cluster — bash sketch)
--   read -s -p 'admin password: ' PGPASSWORD; echo; export PGPASSWORD
--   read -s -p 'new ro password: ' RO_PASS; echo
--   for db in $(psql -h <host> -U <admin> -d postgres -tAc \
--                "SELECT datname FROM pg_database
--                  WHERE datallowconn AND datname NOT IN
--                        ('template0','template1','rdsadmin')"); do
--       echo "==> $db"
--       psql -h <host> -U <admin> -d "$db" \
--            -v ro_password="$RO_PASS" \
--            -f deploy/grant_readonly.sql
--   done
--   unset PGPASSWORD RO_PASS
--
-- IDEMPOTENCY
--   Re-runnable. Existing role's password is reset to the new value (so this
--   doubles as a password-rotation script). All GRANTs are no-ops if already
--   present. DEFAULT PRIVILEGES are upserted for the schemas it iterates.
--
-- RDS NOTES
--   The admin user must have rds_superuser. NOSUPERUSER / NOREPLICATION
--   attribute toggles are NOT issued — defaults of CREATE ROLE already match
--   what we want, and rds_superuser cannot toggle SUPERUSER/REPLICATION bits
--   anyway.
-- =============================================================================

\if :{?ro_password}
\else
  \echo
  \echo 'ERROR: pass -v ro_password=... on the psql command line.'
  \echo '       See the header of this file for the recommended invocation.'
  \quit
\endif

\set ON_ERROR_STOP on

-- 1. Role: create if missing.
SELECT format('CREATE ROLE dba_slackbot_ro LOGIN PASSWORD %L', :'ro_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dba_slackbot_ro')\gexec

-- 2. Always (re-)set the password (rotation path).
SELECT format('ALTER ROLE dba_slackbot_ro WITH LOGIN PASSWORD %L', :'ro_password')\gexec

-- 3. Connection limit so a runaway bot can never starve other clients.
ALTER ROLE dba_slackbot_ro CONNECTION LIMIT 5;

-- 4. CONNECT on the current database.
SELECT format('GRANT CONNECT ON DATABASE %I TO dba_slackbot_ro', current_database())\gexec

-- 4b. pg_read_all_stats: let the bot see pg_stat_statements query text (and the
--     other pg_stat_* views) for statements run by OTHER roles. Without it a
--     non-superuser role gets '<insufficient privilege>' in the query column,
--     so a pg_stat_statements pull from /sql shows nothing useful. This is a
--     cluster-wide role membership (not per-database), idempotent, and guarded
--     so it is a no-op on a server that lacks the predefined role. The admin
--     running this must be a member of pg_read_all_stats WITH ADMIN OPTION —
--     rds_superuser is. The text in pg_stat_statements is normalized (literals
--     become $1/$2), so this exposes query shapes, not data values.
SELECT 'GRANT pg_read_all_stats TO dba_slackbot_ro'
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pg_read_all_stats')\gexec

-- 4b. pg_read_all_data (PG 14+) — SELECT on every table in every schema,
--     including ones created later by any role. This is the durable answer to
--     the whole class of "QueryHub lists a table, then says permission denied
--     for it": the per-schema GRANTs below only cover objects that exist NOW,
--     and ALTER DEFAULT PRIVILEGES only covers objects created by the roles it
--     was set for. Two targets provisioned without this role had 71 relations
--     the RO user could not read while the catalog listed them all.
--     Cluster-wide, idempotent, and a no-op on a server too old to have it.
SELECT 'GRANT pg_read_all_data TO dba_slackbot_ro'
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pg_read_all_data')\gexec

-- 5. For every non-system schema:
--      - GRANT USAGE
--      - GRANT SELECT on existing tables, views, sequences
--      - GRANT EXECUTE on existing functions
--      - DEFAULT PRIVILEGES so future objects auto-inherit SELECT
--
-- A note on the blanket EXECUTE below: the RO role can run every
-- user-defined function in every non-system schema, and a VOLATILE / SECURITY
-- DEFINER function can have write side effects. The primary mitigation is in
-- the executor, NOT here: RO-tier queries now run with
-- default_transaction_read_only=on, so a read-only transaction refuses any
-- data modification even inside a called function. We deliberately keep the
-- broad EXECUTE grant (many read paths call helper functions, and a blanket
-- REVOKE across ~40 prod RDS risks breaking legitimate RO queries). If you
-- want defense in depth beyond the read-only session, scope EXECUTE to a
-- vetted allow-list per schema instead of revoking wholesale — do it as a
-- reviewed per-fleet change, not by editing this bootstrap script.
DO $do$
DECLARE
    s text;
    owner_role text;
    sys_schemas text[] := ARRAY[
        'pg_catalog', 'information_schema', 'pg_toast',
        'pg_temp_1', 'pg_toast_temp_1'
    ];
BEGIN
    FOR s IN
        SELECT nspname
        FROM pg_namespace
        WHERE nspname NOT IN (SELECT unnest(sys_schemas))
          AND nspname NOT LIKE 'pg\_temp\_%'
          AND nspname NOT LIKE 'pg\_toast\_%'
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO dba_slackbot_ro', s);
        EXECUTE format('GRANT SELECT ON ALL TABLES    IN SCHEMA %I TO dba_slackbot_ro', s);
        EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO dba_slackbot_ro', s);
        EXECUTE format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO dba_slackbot_ro', s);

        -- Default privileges: future objects in this schema auto-grant SELECT.
        -- ALTER DEFAULT PRIVILEGES attaches to a CREATING ROLE, so setting it
        -- only for the role running this script silently misses every object
        -- a different role creates later. That is not a corner case: on
        -- one target this left 19 tables (all owned by the same role, all
        -- created after the last run) unreadable, and QueryHub answered
        -- "permission denied for table ..." for a table its own catalog was
        -- happily listing.
        --
        -- So set the defaults for EVERY role that already owns an object in
        -- this schema, plus the current role. Being able to do this requires
        -- membership in those roles; skip the ones we can't set rather than
        -- aborting the whole bootstrap.
        FOR owner_role IN
            SELECT DISTINCT pg_get_userbyid(c.relowner)
            FROM pg_class c
            WHERE c.relnamespace = s::regnamespace
              AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
            UNION
            SELECT current_role
        LOOP
            BEGIN
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                    'GRANT SELECT ON TABLES TO dba_slackbot_ro', owner_role, s);
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                    'GRANT SELECT ON SEQUENCES TO dba_slackbot_ro', owner_role, s);
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                    'GRANT EXECUTE ON FUNCTIONS TO dba_slackbot_ro', owner_role, s);
            EXCEPTION WHEN insufficient_privilege OR undefined_object THEN
                RAISE NOTICE 'default privileges skipped for role % in schema % (%)',
                    owner_role, s, SQLERRM;
            END;
        END LOOP;
    END LOOP;
END
$do$;

-- 6. Sanity check — show what the role can see.
\echo
\echo '== dba_slackbot_ro provisioned in:'
SELECT current_database() AS database;

\echo
\echo '== schemas with USAGE granted:'
SELECT nspname
FROM pg_namespace n
WHERE has_schema_privilege('dba_slackbot_ro', n.nspname, 'USAGE')
  AND nspname NOT IN ('pg_catalog','information_schema','pg_toast')
  AND nspname NOT LIKE 'pg\_temp\_%'
  AND nspname NOT LIKE 'pg\_toast\_%'
ORDER BY nspname;

\echo
\echo '== role attribute summary:'
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolconnlimit
FROM pg_roles WHERE rolname = 'dba_slackbot_ro';

\echo
\echo 'Done. Register this server in the bot DB next:'
\echo '  1. python scripts/encrypt_secret.py    (encrypt the password)'
\echo '  2. INSERT INTO target_servers ... using dba_slackbot_ro and'
\echo '     the ciphertext from step 1. See deploy/db_admin_templates.sql'
\echo '     or docs/OPERATIONS.md section 5.'
