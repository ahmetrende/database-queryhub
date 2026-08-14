-- `target_servers.super_ddl_role`: the role a super-admin's session assumes here.
--
-- The bot's own login on a target is deliberately unprivileged for DDL — it owns
-- nothing, so `ALTER TABLE` fails with 42501. Rather than hand that login
-- standing privileges (which every session would then carry), the elevation is a
-- role the session ENTERS for one statement:
--
--     SET LOCAL ROLE <super_ddl_role>
--
-- The machinery already exists — `team_target_grants.target_role` does the same
-- thing for team grants, and the executor quotes it with pgsql.Identifier. This
-- column is the same idea for the super-admin path, per target because the right
-- role differs per database.
--
-- Two properties this shape buys, and both were verified on the pilot database:
--
--   * membership is granted WITH INHERIT FALSE, so the login carries none of the
--     role's privileges until it explicitly enters it. Users cannot write their
--     own SET ROLE (query_safety refuses every form), so the elevation happens
--     only where the executor puts it, behind the super-admin check.
--   * the role's reach is its own ownership. On the pilot, an elevated session
--     could CREATE/ALTER/DROP inside qh_pilot and was refused even SELECT on
--     audit_log — a bound the credential itself cannot exceed.
--
-- NULL (the default, and every existing row) means no elevation on that target:
-- a super-admin runs there exactly as before.

ALTER TABLE target_servers
    ADD COLUMN IF NOT EXISTS super_ddl_role text;

COMMENT ON COLUMN target_servers.super_ddl_role IS
    'Role a super-admin session enters (SET LOCAL ROLE) on this target so DDL '
    'has ownership. NULL = no elevation. Grant it to the bot login WITH '
    'INHERIT FALSE so it is inert until entered.';
