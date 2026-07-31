-- Per-column SQL types for a delivered result, captured from the driver.
--
-- The web grid's header tooltip was guessing. It built a column-NAME -> type
-- map from the hourly schema snapshot and dropped any name found in two tables
-- with different types, so `id`, `user_id` and `created_at` — the columns people
-- actually hover — were always dropped and the tooltip showed the bare name.
--
-- The driver knows the real answer, but only while the cursor is open: the
-- result is written to a CSV and the web reads that file back in a different
-- process, minutes or hours later. So the types are captured at execution time
-- and stored here, next to `row_count` and `explain_plan` (migration 016 set
-- the JSONB precedent).
--
-- Shape: {"column name": "int4", "amount": "numeric(10,2)", "tags": "int4[]"}.
-- Nullability is NOT in here — psycopg reports null_ok=None for every column,
-- so `not null` still comes from the schema catalog, which does know.
--
-- Nullable and additive: every row written before this migration simply has no
-- types, and the tooltip falls back to the old catalog lookup for those.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS result_column_types JSONB;

COMMENT ON COLUMN requests.result_column_types IS
    'Column name -> SQL type as reported by the driver''s cursor description at '
    'execution time (psycopg type_display / pyodbc type object). Feeds the web '
    'result grid header tooltip. NULL for requests executed before migration '
    '083 and for statements that returned no result set.';
