-- QueryHub — SQL Server (MSSQL) target provisioning (three-tier RO/RW/DDL).
--
-- Mirror of deploy/grant_readonly.sql / grant_readwrite.sql / grant_ddl.sql
-- for a SQL Server target. Creates one least-privilege login per tier and
-- maps it into the target database with the smallest role that covers that
-- tier. Placeholders only — no real hostnames, passwords, or DB names live
-- in the repo (see deploy/NEW_TARGET.md).
--
-- HOST PREREQUISITES (run once on the bot host, not here):
--   - Microsoft ODBC Driver 18 for SQL Server (`msodbcsql18`, apt: Microsoft
--     repo) — the driver name pyodbc connects through.
--   - `pip install '.[mssql]'` (pulls pyodbc) in the bot's venv.
--   The bot stays Postgres-only until both are present AND the target's
--   engine is added to engines.WIRED_ENGINES.
--
-- TIER MODEL (least privilege — never sysadmin / db_owner / CONTROL SERVER):
--   RO  -> db_datareader
--   RW  -> db_datareader + db_datawriter
--   DDL -> db_datareader + db_datawriter + db_ddladmin
-- The bot picks the login matching the query's classified tier, exactly like
-- Postgres. DENY blocks below keep even the DDL login off the dangerous
-- server surface (xp_cmdshell, OLE automation, linked servers).
--
-- Replace <STRONG_PASSWORD_*> and <TARGET_DB> before running. Run the
-- server-level section against `master`, then the database section against
-- the target database.
--
-- AVAILABILITY GROUP (this deployment): the target host is an AG LISTENER.
--   - Read routing: the bot sends RO-tier queries with
--     `ApplicationIntent=ReadOnly`, so the listener routes them to a READABLE
--     SECONDARY; RW/DDL go to the primary. (Ensure the AG has read-only
--     routing configured + the secondary allows read connections.)
--   - Logins do NOT replicate: run the SERVER-scope block on EVERY replica
--     instance, and create the login with the SAME SID on each so the mapped
--     database user still resolves after a failover / on the secondary:
--         -- on primary: SELECT sid FROM sys.sql_logins WHERE name='dba_slackbot_ro';
--         -- on each secondary:
--         CREATE LOGIN dba_slackbot_ro WITH PASSWORD='...', SID=0x<sid-from-primary>, CHECK_POLICY=ON;
--     The DATABASE-scope block runs once on the primary — the users + role
--     memberships replicate with the database.

/* ============================ SERVER SCOPE (master) ====================== */
-- Run this block connected to `master` as a sysadmin — on EVERY replica
-- (matching SIDs, see the AG note above).

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'dba_slackbot_ro')
    CREATE LOGIN dba_slackbot_ro  WITH PASSWORD = '<STRONG_PASSWORD_RO>',  CHECK_POLICY = ON;
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'dba_slackbot_rw')
    CREATE LOGIN dba_slackbot_rw  WITH PASSWORD = '<STRONG_PASSWORD_RW>',  CHECK_POLICY = ON;
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'dba_slackbot_ddl')
    CREATE LOGIN dba_slackbot_ddl WITH PASSWORD = '<STRONG_PASSWORD_DDL>', CHECK_POLICY = ON;

-- Keep every tier off the shell / OLE-automation / advanced-config surface.
-- xp_cmdshell + Ole Automation Procedures should already be disabled via
-- sp_configure; this DENY is belt-and-suspenders at the principal level.
DENY ALTER ANY LOGIN, ALTER ANY SERVER ROLE, ALTER ANY CREDENTIAL,
     ALTER ANY LINKED SERVER, CONTROL SERVER
  TO dba_slackbot_ro, dba_slackbot_rw, dba_slackbot_ddl;

/* ============================ DATABASE SCOPE (<TARGET_DB>) ================ */
-- Run this block connected to the TARGET database.

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'dba_slackbot_ro')
    CREATE USER dba_slackbot_ro  FOR LOGIN dba_slackbot_ro;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'dba_slackbot_rw')
    CREATE USER dba_slackbot_rw  FOR LOGIN dba_slackbot_rw;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'dba_slackbot_ddl')
    CREATE USER dba_slackbot_ddl FOR LOGIN dba_slackbot_ddl;

-- RO: read only.
ALTER ROLE db_datareader ADD MEMBER dba_slackbot_ro;

-- RW: read + write rows (no schema changes).
ALTER ROLE db_datareader ADD MEMBER dba_slackbot_rw;
ALTER ROLE db_datawriter ADD MEMBER dba_slackbot_rw;

-- DDL: read + write + schema objects (CREATE/ALTER/DROP), but NOT db_owner.
ALTER ROLE db_datareader ADD MEMBER dba_slackbot_ddl;
ALTER ROLE db_datawriter ADD MEMBER dba_slackbot_ddl;
ALTER ROLE db_ddladmin   ADD MEMBER dba_slackbot_ddl;

-- Defense in depth: never let the RO login write, even if a future role
-- membership is added by mistake. (App-layer safety already blocks writes
-- on the RO tier; this is the DB-layer backstop.)
DENY INSERT, UPDATE, DELETE, EXECUTE TO dba_slackbot_ro;
