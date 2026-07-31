-- Access-request flow: when a user runs /sql but has no team grants for
-- the target/database they want, they can submit an access request that
-- DMs admins for review.

CREATE TABLE IF NOT EXISTS access_requests (
    id                  BIGSERIAL PRIMARY KEY,
    requester_slack_id  TEXT        NOT NULL,
    requester_name      TEXT,
    target_server_id    INT         REFERENCES target_servers(id) ON DELETE SET NULL,
    database_name       TEXT,
    attempted_query     TEXT,
    reason              TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by_slack_id TEXT,
    decided_by_name     TEXT,
    decision_reason     TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at          TIMESTAMPTZ
);

-- Per-user, per-(target, attempted-query) only ONE pending row at a time.
-- After the row is decided (approved/rejected) the partial filter no longer
-- matches it, so the same user can re-request the same query later.
-- target_server_id can be NULL if the requester didn't pick a target; we
-- coalesce to 0 to keep the index unique key non-null. md5() handles long
-- queries cleanly.
CREATE UNIQUE INDEX IF NOT EXISTS uq_access_requests_pending
    ON access_requests (
        requester_slack_id,
        COALESCE(target_server_id, 0),
        md5(COALESCE(attempted_query, ''))
    )
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_access_requests_pending
    ON access_requests (status, created_at)
    WHERE status = 'pending';

-- Admin DMs for an access request. Same lockstep-update pattern as
-- request_notifications: one row per admin, used by chat.update so all DMs
-- show the resolution when any admin acts.
CREATE TABLE IF NOT EXISTS access_request_notifications (
    id                 BIGSERIAL PRIMARY KEY,
    access_request_id  BIGINT NOT NULL REFERENCES access_requests(id) ON DELETE CASCADE,
    admin_slack_id     TEXT   NOT NULL,
    channel_id         TEXT   NOT NULL,
    message_ts         TEXT   NOT NULL,
    UNIQUE (access_request_id, admin_slack_id)
);
