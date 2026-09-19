-- Amazon Athena as a taggable engine.
--
-- The archive this is for lives in S3 as Parquet and is queried through
-- Athena: the rows are gone from the production database, but a developer
-- still needs to read them occasionally. Routing that through QueryHub means
-- nobody is handed Athena or S3 access of their own -- the query is approved,
-- audited, masked and row-limited exactly like every other target.
--
-- Same shape as 068 added 'mssql': this lets a target be TAGGED athena, which
-- makes its safety profile apply immediately (read-only, Trino dialect, no
-- cross-catalog references), while engines.WIRED_ENGINES keeps it fail-closed
-- -- a tagged target refuses to execute rather than falling back to the
-- Postgres path, which would run it with the wrong dialect and no blocklist.
--
-- Nothing is tagged by this migration. It only widens what the CHECK accepts.
ALTER TABLE target_servers DROP CONSTRAINT IF EXISTS target_servers_engine_check;
ALTER TABLE target_servers ADD CONSTRAINT target_servers_engine_check
  CHECK (engine IN ('postgres', 'mssql', 'clickhouse', 'athena'));
