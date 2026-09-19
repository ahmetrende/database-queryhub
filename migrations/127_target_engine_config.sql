-- Connection settings for an engine that has no host to connect to.
--
-- Every target so far was a server: a host, a port, a username and a password.
-- Athena is none of those. A query is an API call, and what identifies the
-- thing being queried is a region, a workgroup, a catalog and a Glue database;
-- the identity is a role the gateway ASSUMES, so there is no secret to store
-- at all.
--
-- Three columns already nearby were considered and rejected:
--   * `secrets_ref` is for CREDENTIALS -- its contract is "resolve to
--     (username, password)", and Athena has neither. Overloading it would mean
--     the one place that answers "where do this target's credentials come
--     from" sometimes answers something else.
--   * `tags` is display-only by its own migration (095) and is shown to
--     developers; connection settings are not decoration.
--   * `notes` is prose.
--
-- So: one nullable JSONB column, read by the engine that needs it and ignored
-- by every engine that does not. Shape for athena:
--   {"region","workgroup","catalog","database","role_arn"}
-- All four of those are per TARGET, not per engine -- a second archive
-- (a second service is already planned) arrives with its own database and its
-- own workgroup, and must need no code change.
ALTER TABLE target_servers ADD COLUMN IF NOT EXISTS engine_config JSONB;

COMMENT ON COLUMN target_servers.engine_config IS
  $$Per-target connection settings for engines that are not host/port servers. athena: {"region","workgroup","catalog","database","role_arn"}. NULL for postgres/mssql, which use host/port/username/password.$$;

-- username / password_encrypted stop being NOT NULL, because an assumed-role
-- engine has nothing to put in them. A sentinel ('-', '') was the alternative
-- and is worse: a fake credential is a credential somebody eventually tries to
-- connect with.
--
-- The invariant is not dropped, it MOVES: every engine that connects with a
-- credential must still have one. Stated as a CHECK so a postgres target with
-- a missing password is refused at write time exactly as it was before, and so
-- the exception is written down where the column is rather than in someone's
-- memory.
ALTER TABLE target_servers ALTER COLUMN username           DROP NOT NULL;
ALTER TABLE target_servers ALTER COLUMN password_encrypted DROP NOT NULL;

ALTER TABLE target_servers DROP CONSTRAINT IF EXISTS target_servers_credentials_present;
ALTER TABLE target_servers ADD CONSTRAINT target_servers_credentials_present
  CHECK (engine = 'athena' OR (username IS NOT NULL AND password_encrypted IS NOT NULL));
