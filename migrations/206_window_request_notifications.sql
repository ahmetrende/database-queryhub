-- 206: admin DM cards of a window request (2026-11-12).
--
-- One row per admin who got the Approve / Reject card of an auto-approve
-- window request. When one admin decides, or the requester withdraws, the bot
-- reads these rows and replaces every copy of the card with a status line.
-- This is the same pattern as access_request_notifications (migration 005).
-- Safe to run again.

CREATE TABLE IF NOT EXISTS auto_approve_request_notifications (
    id              BIGSERIAL PRIMARY KEY,
    request_id      BIGINT NOT NULL REFERENCES auto_approve_requests(id) ON DELETE CASCADE,
    admin_slack_id  TEXT   NOT NULL,
    channel_id      TEXT   NOT NULL,
    message_ts      TEXT   NOT NULL,
    UNIQUE (request_id, admin_slack_id)
);
