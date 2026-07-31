-- QueryHub — initial schema
-- Apply against the bot's metadata database.

CREATE TABLE IF NOT EXISTS bot_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS target_servers (
    id                 SERIAL PRIMARY KEY,
    alias              TEXT NOT NULL UNIQUE,
    host               TEXT NOT NULL,
    port               INT  NOT NULL DEFAULT 5432,
    default_database   TEXT NOT NULL,
    username           TEXT NOT NULL,
    password_encrypted TEXT NOT NULL,
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    notes              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_target_servers_enabled
    ON target_servers (enabled, alias);

CREATE TABLE IF NOT EXISTS admins (
    slack_user_id TEXT PRIMARY KEY,
    name          TEXT,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_by      TEXT
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'request_status') THEN
        CREATE TYPE request_status AS ENUM (
            'pending',
            'approved',
            'rejected',
            'changes_requested',
            'executing',
            'completed',
            'failed',
            'cancelled'
        );
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS requests (
    id                  BIGSERIAL PRIMARY KEY,
    requester_slack_id  TEXT NOT NULL,
    requester_name      TEXT,
    target_server_id    INT  NOT NULL REFERENCES target_servers(id),
    database_name       TEXT NOT NULL,
    query               TEXT NOT NULL,
    wants_result        BOOLEAN NOT NULL DEFAULT TRUE,
    justification       TEXT,
    status              request_status NOT NULL DEFAULT 'pending',
    decided_by_slack_id TEXT,
    decided_by_name     TEXT,
    decision_reason     TEXT,
    decided_at          TIMESTAMPTZ,
    executed_at         TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    row_count           INT,
    truncated           BOOLEAN NOT NULL DEFAULT FALSE,
    error_message       TEXT,
    csv_file_path       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_requests_pending
    ON requests (status)
    WHERE status IN ('pending', 'approved', 'executing');

CREATE INDEX IF NOT EXISTS idx_requests_requester
    ON requests (requester_slack_id, created_at DESC);

-- Tracks each admin DM so we can update all of them when any admin decides.
CREATE TABLE IF NOT EXISTS request_notifications (
    id              BIGSERIAL PRIMARY KEY,
    request_id      BIGINT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    admin_slack_id  TEXT   NOT NULL,
    channel_id      TEXT   NOT NULL,
    message_ts      TEXT   NOT NULL,
    UNIQUE (request_id, admin_slack_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    request_id      BIGINT REFERENCES requests(id) ON DELETE SET NULL,
    actor_slack_id  TEXT,
    actor_name      TEXT,
    action          TEXT NOT NULL,
    details         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_request
    ON audit_log (request_id, created_at DESC);
