-- Migrate config out of /etc/queryhub/env into the bot DB so all knobs are
-- managed via SQL.
--
--   * `requesters` table replaces the REQUESTER_ALLOWLIST env var. Bot reads
--     it at every /sql; admins always bypass; an EMPTY table means the bot
--     is open to all workspace users (same semantics the env var had).
--   * Email column on `admins` and `requesters` so audit / lookups don't
--     have to round-trip through Slack. Filled lazily by the bot via
--     users.info on first user interaction.
--   * bot_config rows for `log_level` (process startup) and
--     `results_ttl_hours` (cleanup script).

CREATE TABLE IF NOT EXISTS requesters (
    slack_user_id  TEXT PRIMARY KEY
        CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'),
    email          TEXT,
    name           TEXT,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    added_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_by       TEXT
);

CREATE INDEX IF NOT EXISTS idx_requesters_enabled
    ON requesters (enabled, slack_user_id);

ALTER TABLE admins ADD COLUMN IF NOT EXISTS email TEXT;

INSERT INTO bot_config (key, value, description) VALUES
    ('log_level', 'INFO',
     'Python logging level for the bot process. Read at startup; restart to apply.'),
    ('results_ttl_hours', '72',
     'How long Slack file uploads + local CSV results are kept before cleanup deletes them. Kept short to limit the window sensitive result data lives in Slack.')
ON CONFLICT (key) DO NOTHING;
