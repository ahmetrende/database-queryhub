-- Functions and procedures in the schema catalog.
--
-- The snapshot has always recorded tables and columns, so autocomplete could
-- offer both and never offered a function — not `count`, not a stored procedure,
-- not the operator's own helpers. Every suggestion pool was also effectively
-- Postgres-shaped, because nothing carried an engine dimension into the catalog.
--
-- Same shape and lifecycle as schema_tables: one row per routine per (target,
-- database), refreshed by delete+insert inside the snapshot's transaction, so a
-- reader never sees half a catalog. Nothing here is used for authorization —
-- suggesting a name is not permission to call it, and the safety layer still
-- decides what may run (engines.blocked_functions is checked on the AST).

CREATE TABLE IF NOT EXISTS schema_functions (
    id               bigserial PRIMARY KEY,
    target_server_id int  NOT NULL REFERENCES target_servers(id),
    database_name    text NOT NULL,
    schema_name      text NOT NULL,
    routine_name     text NOT NULL,
    -- function | procedure | aggregate | window: shown as the suggestion's kind
    -- so a procedure is not offered where an expression belongs.
    routine_kind     text NOT NULL,
    -- Rendered argument list and return type, for the hover only. Kept as text
    -- because they are display, and because normalising type names across
    -- engines buys nothing here.
    arg_signature    text,
    returns          text,
    snapshot_at      timestamptz NOT NULL DEFAULT now(),
    -- Overloads collapse: two functions with the same name in the same schema are
    -- one suggestion. The signature of whichever row lands first is kept, which
    -- is honest for a typeahead and avoids five `count` entries.
    UNIQUE (target_server_id, database_name, schema_name, routine_name)
);

CREATE INDEX IF NOT EXISTS ix_schema_functions_lookup
    ON schema_functions (target_server_id, database_name, routine_name);
