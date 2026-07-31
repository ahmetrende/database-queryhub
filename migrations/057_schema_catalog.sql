-- Schema catalog: an hourly snapshot of every target's tables + columns,
-- taken with the RO credential and stored in the bot DB. Powers the
-- schema browser in the /sql modal and the tables / schema / findcol
-- subcommands without touching the targets at browse time.
--
-- Partitions are collapsed at snapshot time: one row per partitioned
-- parent (with partition_count / partition_key), never one per child.
-- Refresh is a delete+insert swap per (target, database) in one
-- transaction, so readers never see a half-written snapshot.

CREATE TABLE IF NOT EXISTS schema_tables (
    id               bigserial PRIMARY KEY,
    target_server_id int  NOT NULL REFERENCES target_servers(id),
    database_name    text NOT NULL,
    schema_name      text NOT NULL,
    table_name       text NOT NULL,
    relkind          text NOT NULL,  -- table | partitioned | matview | view | foreign
    row_estimate     bigint,
    total_bytes      bigint,
    partition_count  int,
    partition_key    text,           -- e.g. HASH (user_id)
    indexes          jsonb,          -- [{name, def}, ...]
    foreign_keys     jsonb,          -- [{name, def}, ...]
    snapshot_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (target_server_id, database_name, schema_name, table_name)
);

CREATE TABLE IF NOT EXISTS schema_columns (
    id          bigserial PRIMARY KEY,
    table_id    bigint NOT NULL REFERENCES schema_tables(id) ON DELETE CASCADE,
    ordinal     int  NOT NULL,
    column_name text NOT NULL,
    data_type   text NOT NULL,
    not_null    boolean NOT NULL DEFAULT false,
    default_expr text,
    is_pk       boolean NOT NULL DEFAULT false,
    in_index    boolean NOT NULL DEFAULT false
);

-- Typeahead + detail lookups.
CREATE INDEX IF NOT EXISTS ix_schema_tables_lookup
    ON schema_tables (target_server_id, database_name, table_name);
-- findcol across the fleet.
CREATE INDEX IF NOT EXISTS ix_schema_columns_table
    ON schema_columns (table_id);
CREATE INDEX IF NOT EXISTS ix_schema_columns_name
    ON schema_columns (column_name);
