-- 218: show an approver what uses an object before a DROP or ALTER.
--
-- A DDL request that drops or alters a table, view, materialized view,
-- function or procedure can break the objects that use it. At submit time
-- QueryHub reads the catalog of the target with the read-only login and
-- stores the objects that depend on each named object. The approval card in
-- Slack and the web approval queue show the list (ddl_dependents.py).
--
-- NULL means "not checked": the request is not a DROP or ALTER, the switch
-- pre_flight_explain is off, or the read failed. The read never blocks or
-- fails a submit.

ALTER TABLE requests ADD COLUMN IF NOT EXISTS ddl_dependents JSONB;

COMMENT ON COLUMN requests.ddl_dependents IS
    'Objects that use each object a DROP or ALTER names, read at submit '
    '(ddl_dependents.read). NULL means not checked (migration 218).';
