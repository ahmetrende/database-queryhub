-- Two new dimensions on pii_masking_exemptions: a schema, and a super-admin gate.
--
-- The masking catalog works on column NAMES, which is why the operator's own
-- monitoring toolkit gets mangled: a `dba` schema view exposing query text, host
-- names or session owners trips the same rules a customer table does. The values
-- are infrastructure telemetry, not personal data, and the person reading them is
-- the one who installed them.
--
-- Both dimensions are needed and neither is enough alone:
--
--   * schema_name -- because the existing table_name dimension matches on the
--     BARE name. `dba.blocking_sessions` and `public.blocking_sessions` are the
--     same string to it, so a table-scoped exemption for a toolkit view would
--     silently unmask a business table that happens to share its name.
--
--   * super_admin_only -- because an exemption is otherwise fleet-wide for
--     everyone. Lifting masking on `dba` for all readers would hand query text
--     (pg_stat_statements holds literals) to anyone with a grant.
--
-- target_server_id becomes nullable so one row can cover the fleet. Previously
-- "every server" meant one row per server, which drifts the moment a server is
-- added -- exactly how a new target ends up with different masking from its
-- siblings and nobody notices.

ALTER TABLE pii_masking_exemptions
    ADD COLUMN IF NOT EXISTS schema_name      text,
    ADD COLUMN IF NOT EXISTS super_admin_only boolean NOT NULL DEFAULT false;

ALTER TABLE pii_masking_exemptions
    ALTER COLUMN target_server_id DROP NOT NULL;

COMMENT ON COLUMN pii_masking_exemptions.schema_name IS
    'Schema this exemption is scoped to, matched only against explicitly '
    'qualified references. NULL = not schema-scoped. Fail-closed: a query that '
    'names a table without its schema cannot claim a schema-scoped exemption, '
    'because provenance is unprovable -- search_path could resolve it anywhere.';

COMMENT ON COLUMN pii_masking_exemptions.super_admin_only IS
    'When true the exemption applies only to super-admins (a permanent admin '
    'row with every scope column NULL). Everyone else keeps full masking.';

COMMENT ON COLUMN pii_masking_exemptions.target_server_id IS
    'NULL = every target. Set = that target only.';

-- The operator's monitoring toolkit, fleet-wide, super-admins only.
INSERT INTO pii_masking_exemptions
    (target_server_id, database_name, schema_name, table_name, column_name,
     super_admin_only, reason, enabled, created_by)
SELECT NULL, NULL, 'dba', NULL, NULL, true,
       'Operator monitoring toolkit (dba.* views on every database of every '
       'server): infrastructure telemetry, not personal data, and the masking '
       'catalog mangles it because it matches column names. Super-admins only, '
       'and only for explicitly dba-qualified references.',
       true, 'migration 090'
WHERE NOT EXISTS (
    SELECT 1 FROM pii_masking_exemptions
     WHERE schema_name = 'dba' AND target_server_id IS NULL
       AND database_name IS NULL AND table_name IS NULL AND column_name IS NULL
);
