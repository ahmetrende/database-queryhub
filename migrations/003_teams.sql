-- Per-team authorization: which Slack users can submit /sql against which
-- target servers (and optionally which databases on those targets).
--
-- Operate via plain INSERT / DELETE / UPDATE — no CLI required. Foreign keys
-- and the CHECK on slack_user_id format do the validation.
--
-- Examples:
--
--   -- 1) Create a team:
--   INSERT INTO teams (name, description) VALUES ('payment', 'Payment squad');
--
--   -- 2) Add members (Slack user IDs from Profile → "..." → Copy member ID):
--   INSERT INTO team_members (team_id, slack_user_id) VALUES
--       ((SELECT id FROM teams WHERE name = 'payment'), 'U0PAYMENT01'),
--       ((SELECT id FROM teams WHERE name = 'payment'), 'U0PAYMENT02');
--
--   -- 3) Grant the team access to a target. allowed_databases = NULL means
--   --    every database on that target; non-empty array means ONLY those.
--   INSERT INTO team_target_grants (team_id, target_server_id, allowed_databases) VALUES
--       ((SELECT id FROM teams WHERE name = 'payment'), 5, NULL),
--       ((SELECT id FROM teams WHERE name = 'payment'), 6, ARRAY['payment_db','orders_db']);
--
--   -- 4) Inspect:
--   SELECT * FROM v_user_targets WHERE slack_user_id = 'U0PAYMENT01';
--   SELECT * FROM v_team_summary;

CREATE TABLE IF NOT EXISTS teams (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL CHECK (length(name) BETWEEN 2 AND 64),
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id        INT  NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    slack_user_id  TEXT NOT NULL CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'),
    added_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (team_id, slack_user_id)
);

CREATE INDEX IF NOT EXISTS idx_team_members_user
    ON team_members (slack_user_id);

CREATE TABLE IF NOT EXISTS team_target_grants (
    team_id            INT  NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    target_server_id   INT  NOT NULL REFERENCES target_servers(id) ON DELETE CASCADE,
    -- NULL  = every database on this target is allowed
    -- empty = same as NULL (treat as "no restriction")
    -- non-empty = only these database names allowed (case-sensitive match)
    allowed_databases  TEXT[],
    granted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (team_id, target_server_id),
    -- Forbid weird ['']: arrays must be NULL or contain only non-empty strings.
    CHECK (
        allowed_databases IS NULL
        OR NOT (allowed_databases && ARRAY[''])
    )
);

CREATE INDEX IF NOT EXISTS idx_team_target_grants_target
    ON team_target_grants (target_server_id);

-- Convenience view: "who can reach what". One row per (user, allowed target).
CREATE OR REPLACE VIEW v_user_targets AS
SELECT DISTINCT
    tm.slack_user_id,
    ts.id      AS target_id,
    ts.alias   AS target_alias,
    ts.host,
    ts.default_database,
    g.allowed_databases
FROM team_members tm
JOIN team_target_grants g ON g.team_id = tm.team_id
JOIN target_servers ts    ON ts.id = g.target_server_id AND ts.enabled
ORDER BY tm.slack_user_id, ts.alias;

-- Convenience view: team size + grant count.
CREATE OR REPLACE VIEW v_team_summary AS
SELECT
    t.id,
    t.name,
    t.description,
    (SELECT count(*) FROM team_members      tm WHERE tm.team_id      = t.id) AS member_count,
    (SELECT count(*) FROM team_target_grants g  WHERE g.team_id       = t.id) AS grant_count,
    t.created_at
FROM teams t
ORDER BY t.name;
