-- Pluggable secrets provider per target.
--
-- Default behavior is unchanged: NULL (or 'local') means a target's per-tier
-- credentials come from the Fernet-encrypted *_encrypted columns already on
-- target_servers (the local vault) — no cloud dependency. A target may instead
-- name an external provider (e.g. 'awssm') and carry a provider-specific
-- reference in secrets_ref, so its DB credentials are fetched at execution time
-- and the ciphertext never lives in this metadata DB.
--
-- The provider is resolved inside targets.get_credentials(), so every caller
-- (executor, mssql exec, pre-flight) benefits with no change.
ALTER TABLE target_servers ADD COLUMN IF NOT EXISTS secrets_provider TEXT;
ALTER TABLE target_servers ADD COLUMN IF NOT EXISTS secrets_ref JSONB;

COMMENT ON COLUMN target_servers.secrets_provider IS
  $$Credential source. NULL/'local' = Fernet-encrypted *_encrypted columns (default, zero-dependency); 'awssm' = AWS Secrets Manager via secrets_ref.$$;
COMMENT ON COLUMN target_servers.secrets_ref IS
  $$Provider-specific reference (JSON). awssm: {"secret_id":"<arn-or-name>","region":"<optional>"} where the secret value is JSON {"ro":{"username","password"},"rw":{...},"ddl":{...}}.$$;
