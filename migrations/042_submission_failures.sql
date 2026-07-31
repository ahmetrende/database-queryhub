-- Modal submission validation failures, logged for forensics.
--
-- When a /sql (single) or /sql batch submission is rejected with
-- Slack's validation_errors response action, we record it here. No
-- admin DM — this is a quiet log the operator greps when a user says
-- "I couldn't submit my query". Captures who, when, the error map
-- (block_id -> message), and a best-effort query / target snapshot.

CREATE TABLE IF NOT EXISTS submission_failures (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    slack_user_id       TEXT NOT NULL,
    slack_user_name     TEXT,
    mode                TEXT NOT NULL,          -- 'single' | 'batch'
    target_server_id    INTEGER,                -- best-effort; NULL if unparseable
    database_name       TEXT,
    query               TEXT,                   -- best-effort snapshot of the SQL
    errors              JSONB NOT NULL          -- {block_id: message, ...}
);

CREATE INDEX IF NOT EXISTS idx_submission_failures_user_time
    ON submission_failures (slack_user_id, created_at DESC);

COMMENT ON TABLE submission_failures IS
$$Validation failures from the /sql single + batch modals. Logged for
operator forensics ("why couldn't I submit?"); no admin notification.$$;
