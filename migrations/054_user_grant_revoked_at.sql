-- 054: soft-revocable user grants (stale-grant reaper)
--
-- Adds a soft-disable to user_target_grants so the stale-grant reaper can
-- revoke idle grants reversibly (row + config + audit kept). Every read of
-- user_target_grants (teams.py + this view) filters revoked_at IS NULL, so a
-- revoked grant authorizes nothing. Re-granting (admin) clears revoked_at.
ALTER TABLE user_target_grants ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;

COMMENT ON COLUMN user_target_grants.revoked_at IS
$$NULL = active. Set by the stale-grant reaper (scripts/reap_stale_grants.py) when the user hasn't queried the target in grant_idle_revoke_days. Soft-disable — the row is kept; all reads filter revoked_at IS NULL.$$;

-- View recreated to filter revoked grants out of the user_g CTE (columns
-- unchanged, so CREATE OR REPLACE is safe + re-runnable).
CREATE OR REPLACE VIEW v_effective_user_grants AS
WITH user_g AS (
    SELECT user_target_grants.slack_user_id,
           user_target_grants.target_server_id,
           user_target_grants.allowed_databases,
           user_target_grants.mode,
           'user'::text AS source
      FROM user_target_grants
     WHERE user_target_grants.revoked_at IS NULL
), team_g AS (
    SELECT tm.slack_user_id,
           g.target_server_id,
           CASE
               WHEN bool_or(g.allowed_databases IS NULL OR cardinality(g.allowed_databases) = 0) THEN NULL::text[]
               ELSE array_agg(DISTINCT db.db ORDER BY db.db) FILTER (WHERE db.db IS NOT NULL)
           END AS allowed_databases,
           CASE
               WHEN bool_or(g.mode = 'ddl'::text) THEN 'ddl'::text
               WHEN bool_or(g.mode = 'rw'::text) THEN 'rw'::text
               ELSE 'ro'::text
           END AS mode,
           'team'::text AS source
      FROM team_target_grants g
      JOIN team_members tm ON tm.team_id = g.team_id
      LEFT JOIN LATERAL unnest(g.allowed_databases) db(db) ON true
     GROUP BY tm.slack_user_id, g.target_server_id
)
SELECT COALESCE(u.slack_user_id, t.slack_user_id) AS slack_user_id,
       COALESCE(u.target_server_id, t.target_server_id) AS target_server_id,
       COALESCE(u.allowed_databases, t.allowed_databases) AS allowed_databases,
       COALESCE(u.mode, t.mode) AS mode,
       CASE WHEN u.slack_user_id IS NOT NULL THEN 'user'::text ELSE 'team'::text END AS source
  FROM user_g u
  FULL JOIN team_g t ON u.slack_user_id = t.slack_user_id AND u.target_server_id = t.target_server_id;

INSERT INTO bot_config (key, value, description) VALUES
  ('grant_reaper_enabled', 'off', 'Stale-grant reaper master switch (on|off). Off = the reaper only reports (dry-run); on = it soft-revokes idle user grants.'),
  ('grant_idle_revoke_days', '30', 'Stale-grant reaper: revoke a user_target_grant when the user has not queried that target in this many days (grant must also be older than this).')
ON CONFLICT (key) DO NOTHING;
