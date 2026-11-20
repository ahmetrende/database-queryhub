-- 219: keep table and column comments in the schema catalog.
--
-- PostgreSQL keeps a comment on a table (obj_description) and on a column
-- (col_description). SQL Server keeps the same text in the MS_Description
-- extended property. The hourly catalog snapshot now reads both and stores
-- them here (schema_catalog.py, mssql_exec.py).
--
-- The web schema route sends a comment only to a user who already passes its
-- grant check on that database. The editor hover, the schema tree and the
-- results grid header show it. The AI assistant and the MCP door do not read
-- these columns: a comment can hold sensitive text.
--
-- The snapshot strips each comment, stores NULL for an empty one and cuts a
-- long one at 1000 characters. NULL is the behaviour before this migration.

ALTER TABLE schema_tables
    ADD COLUMN IF NOT EXISTS comment TEXT;

ALTER TABLE schema_columns
    ADD COLUMN IF NOT EXISTS comment TEXT;

COMMENT ON COLUMN schema_tables.comment IS
    'The table or view comment from the target catalog, at most 1000 '
    'characters. NULL when there is none (migration 219).';

COMMENT ON COLUMN schema_columns.comment IS
    'The column comment from the target catalog, at most 1000 characters. '
    'NULL when there is none (migration 219).';
