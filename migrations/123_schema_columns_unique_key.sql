-- A natural key for `schema_columns`, so the hourly catalog can diff.
--
-- The catalog refresh deleted every row for a (target, database) and inserted
-- the lot back, once an hour, whether anything had changed or not. Measured on
-- this instance before the change: 172,450,815 rows inserted into a table that
-- holds 156,736 -- about 1,100 full rewrites, ~3.8M rows a day, plus the same
-- again in dead tuples for autovacuum to carry away. A schema does not change
-- 3.8 million times a day; almost every one of those writes replaced a row
-- with a byte-identical copy.
--
-- Diffing needs a key to conflict on, and the table had none: only `id`, which
-- the delete-and-reinsert cycle regenerated every hour. `(table_id,
-- column_name)` is the real identity of a column -- a table cannot hold two
-- columns of the same name -- and it is already unique in the live data (0
-- duplicate pairs across all 156,736 rows).
--
-- `ordinal` deliberately stays OUT of the key. A column that moves position is
-- the same column, and keying on the position would make an ALTER that
-- reorders a table look like every column being dropped and recreated.

CREATE UNIQUE INDEX IF NOT EXISTS ux_schema_columns_table_column
    ON schema_columns (table_id, column_name);
