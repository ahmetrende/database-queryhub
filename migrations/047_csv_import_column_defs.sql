-- Optional user-supplied column definitions for new-table imports.
--
-- By default a new import table is all TEXT. A user can instead supply
-- typed column definitions ("id int, amount numeric(10,2)"); we parse +
-- type-allowlist them (no raw SQL) and CREATE the table with those
-- types. NULL = the all-TEXT default.

ALTER TABLE csv_imports
    ADD COLUMN IF NOT EXISTS column_defs JSONB;

COMMENT ON COLUMN csv_imports.column_defs IS
$$User-supplied typed column definitions for a new-table import, as a
JSON array of [name, type] pairs (types validated against an allow-list).
NULL means the columns default to all TEXT.$$;
