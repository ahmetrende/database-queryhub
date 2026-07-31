-- 053: opt-in join behaviour for table+column-scoped PII exemptions
--
-- A table-scoped column exemption normally fires ONLY when the query reads
-- solely that table (provenance is certain). With apply_in_joins=TRUE the row
-- opts into firing whenever its table is among the query's tables — accepting
-- that a same-named column from a co-joined table is also unmasked. Set it
-- only for columns known non-sensitive db-wide (e.g. event/catalog titles).
ALTER TABLE pii_masking_exemptions
    ADD COLUMN IF NOT EXISTS apply_in_joins BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN pii_masking_exemptions.apply_in_joins IS
$$When TRUE, a table+column-scoped exemption also applies in JOINs (fires when its table is among the query's tables, not only when the query reads solely that table). Accepts that a same-named column from a co-joined table is also unmasked — set only for columns that are non-sensitive db-wide. Default FALSE = strict / fail-closed on joins.$$;
