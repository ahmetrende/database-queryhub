-- Migration 050: pii_masking_exemptions — scoped opt-outs from PII masking
--
-- Some targets hold public data (e.g. an OpenSanctions mirror) where
-- masking person names is technically correct but business-wise wrong.
-- This table lets an operator exempt masking at three independent
-- granularities; NULL columns are wildcards:
--
--   (target, NULL, NULL,  NULL)  -> whole target exempt
--   (target, db,   NULL,  NULL)  -> whole database exempt
--   (target, db,   table, NULL)  -> one table exempt (query must reference
--                                   ONLY exempt tables — joins with
--                                   non-exempt tables keep masking ON)
--   (target, db,   table, column) / (target, db, NULL, column)
--                                -> one result column exempt (by name)
--
-- Table-level matching parses the query with sqlglot; if the SQL can't be
-- parsed the exemption does NOT apply (fail-closed, masking stays on).
-- Read per execution — runtime-effective, no restart.

CREATE TABLE IF NOT EXISTS pii_masking_exemptions (
    id                BIGSERIAL    PRIMARY KEY,
    target_server_id  INT          NOT NULL
                      REFERENCES target_servers(id) ON DELETE CASCADE,
    database_name     TEXT,        -- NULL = all databases on the target
    table_name        TEXT,        -- NULL = not table-scoped
    column_name       TEXT,        -- NULL = not column-scoped
    reason            TEXT,
    enabled           BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by        TEXT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_pii_masking_exemptions_scope
    ON pii_masking_exemptions (
        target_server_id,
        COALESCE(database_name, ''),
        COALESCE(table_name, ''),
        COALESCE(column_name, '')
    );

COMMENT ON TABLE pii_masking_exemptions IS
$$Scoped opt-outs from result PII masking, for targets/databases/tables/
columns that hold public data (e.g. OpenSanctions). NULL scope columns are
wildcards. Table-level rows only apply when the query references ONLY
exempt tables (fail-closed on joins / unparseable SQL). Checked per
execution — runtime-effective.$$;
