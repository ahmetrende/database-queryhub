-- Three-tier mode system on grants: ro (default) | rw | ddl.
--
--   - ro:  SELECT, EXPLAIN, SHOW, WITH...SELECT, VALUES, TABLE, FETCH,
--          BEGIN/COMMIT/ROLLBACK
--   - rw:  + INSERT, UPDATE, DELETE, MERGE, COPY (any direction)
--   - ddl: + CREATE, ALTER, DROP, TRUNCATE, COMMENT, GRANT, REVOKE,
--          VACUUM, ANALYZE, REINDEX, CLUSTER, REFRESH, REASSIGN
--
-- Bot behaviour:
--   - For each query, query_safety.required_mode() classifies it ro/rw/ddl.
--   - The user's effective mode for (target) is computed:
--       * If user_target_grants has a row for this (user, target), that
--         row's mode is used (user override beats team).
--       * Otherwise the most permissive of the user's team_target_grants
--         on this target (ro < rw < ddl) — e.g., team RW => members RW.
--       * No grants => no access.
--   - Bot picks credentials from target_servers (username,password) for ro,
--     (username_rw,password_rw_encrypted) for rw, (username_ddl,
--     password_ddl_encrypted) for ddl. Missing credentials at the chosen
--     tier => fail-fast with "Target X is not ready for <mode> queries".
--
-- DDL provisioning (cluster-side) is the DBA's job — Postgres has no
-- built-in pg_alter_all_tables role. Typical paths: schema-ownership
-- transfer, per-schema GRANT CREATE/ALTER/DROP, or rds_superuser
-- membership for `dba_slackbot_ddl`.

-- 1. Mode column on team grants (default 'ro')
ALTER TABLE team_target_grants
    ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'ro'
    CHECK (mode IN ('ro', 'rw', 'ddl'));

COMMENT ON COLUMN team_target_grants.mode IS
$$Permission tier: 'ro' (default), 'rw' (adds write), or 'ddl' (adds schema/maintenance ops). Bot picks credentials from target_servers based on the EFFECTIVE mode for (user, target) — see migration 014's header.$$;

-- 2. User-level grants — override team grants for this specific user
CREATE TABLE IF NOT EXISTS user_target_grants (
    slack_user_id     TEXT NOT NULL
        CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'),
    target_server_id  INT  NOT NULL REFERENCES target_servers(id) ON DELETE CASCADE,
    allowed_databases TEXT[],
    mode              TEXT NOT NULL DEFAULT 'ro'
        CHECK (mode IN ('ro', 'rw', 'ddl')),
    granted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_by        TEXT,
    PRIMARY KEY (slack_user_id, target_server_id),
    CHECK (
        allowed_databases IS NULL
        OR NOT (allowed_databases && ARRAY[''])
    )
);

COMMENT ON TABLE user_target_grants IS
$$Per-user overrides on top of team_target_grants. If a row exists here for (slack_user_id, target_server_id), it ENTIRELY supersedes any team grants the user might have on that target — both allowed_databases and mode. Use to: (a) give a user access to a target their team doesn't have, (b) restrict a user to ro on a target where their team is rw, (c) elevate a single user to ddl without elevating the whole team.$$;

CREATE INDEX IF NOT EXISTS idx_user_target_grants_target
    ON user_target_grants (target_server_id);

-- 3. RW + DDL credentials on target_servers
ALTER TABLE target_servers
    ADD COLUMN IF NOT EXISTS username_rw TEXT;
ALTER TABLE target_servers
    ADD COLUMN IF NOT EXISTS password_rw_encrypted TEXT;
ALTER TABLE target_servers
    ADD COLUMN IF NOT EXISTS username_ddl TEXT;
ALTER TABLE target_servers
    ADD COLUMN IF NOT EXISTS password_ddl_encrypted TEXT;

COMMENT ON COLUMN target_servers.username_rw IS
$$RW user (typically dba_slackbot_rw with pg_read_all_data + pg_write_all_data). Used when the user submits a write query (UPDATE/DELETE/INSERT/MERGE/COPY) and has a grant with mode='rw' or 'ddl'. NULL = RW not configured for this target; write queries get rejected.$$;

COMMENT ON COLUMN target_servers.password_rw_encrypted IS
$$Fernet ciphertext of the RW user's password (use scripts/encrypt_secret.py).$$;

COMMENT ON COLUMN target_servers.username_ddl IS
$$DDL user (typically dba_slackbot_ddl with elevated privileges). Used when the user submits a schema/maintenance query (CREATE/ALTER/DROP/TRUNCATE/VACUUM/...) and has a grant with mode='ddl'. NULL = DDL not configured.$$;

COMMENT ON COLUMN target_servers.password_ddl_encrypted IS
$$Fernet ciphertext of the DDL user's password.$$;

-- 4. Convenience view: effective grant per (user, target)
-- Picks user_target_grants if exists, else aggregates team_target_grants
-- with most-permissive mode (α semantic). Surfaces what the bot will
-- actually use at runtime — handy for debugging "why can/can't this
-- user run X?".
CREATE OR REPLACE VIEW v_effective_user_grants AS
WITH user_g AS (
    SELECT
        slack_user_id,
        target_server_id,
        allowed_databases,
        mode,
        'user'::text AS source
    FROM user_target_grants
),
team_g AS (
    SELECT
        tm.slack_user_id,
        g.target_server_id,
        -- union of team allowed_databases — NULL/empty in any grant
        -- means unrestricted; we represent that as NULL here.
        CASE
            WHEN bool_or(g.allowed_databases IS NULL OR cardinality(g.allowed_databases) = 0)
                THEN NULL
            ELSE array_agg(DISTINCT db ORDER BY db) FILTER (WHERE db IS NOT NULL)
        END AS allowed_databases,
        -- α semantic: most-permissive mode wins (ddl > rw > ro)
        CASE
            WHEN bool_or(g.mode = 'ddl') THEN 'ddl'
            WHEN bool_or(g.mode = 'rw')  THEN 'rw'
            ELSE 'ro'
        END AS mode,
        'team'::text AS source
    FROM team_target_grants g
    JOIN team_members tm ON tm.team_id = g.team_id
    LEFT JOIN LATERAL unnest(g.allowed_databases) AS db ON TRUE
    GROUP BY tm.slack_user_id, g.target_server_id
)
SELECT
    COALESCE(u.slack_user_id, t.slack_user_id)      AS slack_user_id,
    COALESCE(u.target_server_id, t.target_server_id) AS target_server_id,
    COALESCE(u.allowed_databases, t.allowed_databases) AS allowed_databases,
    COALESCE(u.mode, t.mode)                        AS mode,
    CASE WHEN u.slack_user_id IS NOT NULL THEN 'user' ELSE 'team' END AS source
FROM user_g u
FULL OUTER JOIN team_g t
    ON  u.slack_user_id    = t.slack_user_id
    AND u.target_server_id = t.target_server_id;

COMMENT ON VIEW v_effective_user_grants IS
$$Effective (user, target, mode, allowed_databases) the bot computes at runtime. user_target_grants overrides team_target_grants per-(user,target). Multi-team conflicts resolve to most-permissive mode (α: team RW => members RW). source column shows whether the row came from a user override or aggregated team grants.$$;
