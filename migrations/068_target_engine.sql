-- Engine dispatch for multi-engine targets.
--
-- Each target runs on a specific database engine. Until now everything is
-- PostgreSQL; this column lets the bot pick a per-engine strategy (driver,
-- SQL-safety dialect, dangerous-function blocklist, result handling) instead
-- of assuming libpq/psycopg everywhere. Postgres is the default, so every
-- existing target keeps its exact current behavior. See src/queryhub/
-- engines.py for the per-engine specs; SQL Server ('mssql') is added here so
-- a target can be TAGGED with it (its safety profile applies immediately),
-- but engines.WIRED_ENGINES keeps it fail-closed (unexecutable) until its
-- driver + execution path are validated against a real host.
ALTER TABLE target_servers ADD COLUMN IF NOT EXISTS engine TEXT NOT NULL DEFAULT 'postgres';

-- Constrain to engines the bot has a spec for. Drop+add so the migration
-- stays re-runnable and a future engine is a one-line edit.
ALTER TABLE target_servers DROP CONSTRAINT IF EXISTS target_servers_engine_check;
ALTER TABLE target_servers ADD CONSTRAINT target_servers_engine_check
  CHECK (engine IN ('postgres', 'mssql', 'clickhouse'));
