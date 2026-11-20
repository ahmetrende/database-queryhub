-- A requester can name a default schema, per request. PostgreSQL only.
-- Unqualified names in the statement resolve to this schema first. The
-- executor puts it in search_path. It never changes the grant, the tier,
-- the approval rules or the masking. NULL means the old pinned path.
ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS default_schema text;

COMMENT ON COLUMN requests.default_schema IS
    'Schema that unqualified names resolve to first (PostgreSQL only). NULL = default search_path.';
