-- Optional team→role binding on the target. When set, the bot does
-- `SET LOCAL ROLE <role>` inside the query transaction so Postgres enforces
-- the team's privileges on the target cluster (instead of running as the
-- bot's login user with whatever it has).
--
-- See `deploy/grant_team_role.sql` for provisioning the role on the target
-- side (CREATE ROLE + GRANT membership to the bot's login user). The role
-- name MUST exist on the target cluster AND the bot's login user MUST be a
-- member of it, otherwise `SET LOCAL ROLE` throws and the request fails.
--
-- Naming convention (recommended): slackbot_team_<team_name> — but any
-- valid Postgres identifier works.

ALTER TABLE team_target_grants
    ADD COLUMN IF NOT EXISTS target_role TEXT
    CHECK (
        target_role IS NULL
        OR target_role ~ '^[a-z_][a-z0-9_]{0,62}$'
    );

COMMENT ON COLUMN team_target_grants.target_role IS
$$Optional Postgres role name to SET LOCAL ROLE on the target before executing the user's query. NULL = run as the bot's login user (target_servers.username). The role must exist on the target cluster and the login user must have membership granted (deploy/grant_team_role.sql).$$;
