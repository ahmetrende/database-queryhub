-- 064_web_saved_sessions.sql
-- Server-synced named workspaces for QueryHub Web (the Sessions panel).
-- Only dest="server" sessions live here; dest="local" sessions stay in the
-- browser and are NEVER sent to the server. Upsert by (slack_user_id, name)
-- so re-saving the same workspace overwrites instead of duplicating. A
-- retention job purges rows untouched for 30 days (see cleanup_old_results.py).
-- Web-only: the Slack bot never reads this table.

CREATE TABLE IF NOT EXISTS web_saved_sessions (
    id            BIGSERIAL PRIMARY KEY,
    slack_user_id TEXT        NOT NULL,
    name          TEXT        NOT NULL,
    tabs          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (slack_user_id, name)
);

CREATE INDEX IF NOT EXISTS ix_web_saved_sessions_user
    ON web_saved_sessions (slack_user_id, updated_at DESC);

COMMENT ON TABLE web_saved_sessions IS
    'QueryHub Web server-synced named workspaces (Sessions panel). '
    'dest="server" only; local sessions stay in the browser. 30-day retention.';
COMMENT ON COLUMN web_saved_sessions.tabs IS
    'Array of {name, sql, connectionId, databaseId} — the workspace tabs.';
