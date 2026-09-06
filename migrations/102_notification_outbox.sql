-- Outbox for panel-facing notifications: the IDP panel has no DB of its own
-- on this side of the seam, so it cannot compute "which admins should be
-- told about this request" — that is a scope decision (admins.can_approve
-- against the request's tier, target and requester team) that lives here.
-- QueryHub decides recipients and writes one row per event; the panel polls
-- GET/POST /admin/notifications/outbox (a later PR), delivers in-app and via
-- Slack, and stamps processed_at.
--
-- Not the same thing as auth_event_outbox (migration 060): that one is
-- TRIGGER-fed, captures authorization *changes* on arbitrary tables, and is
-- already drained by an in-process poller that stamps its own processed_at.
-- A second consumer racing that poller over the same rows would turn
-- at-least-once delivery into at-least-twice. This table is written by
-- application code, for approval-notification events only, and is read by
-- nothing inside this process — the panel is its only consumer.
--
-- recipients stores whatever admins.list_active() yields (slack_user_id),
-- NOT email. admins.list_active() carries no email column, and either
-- widening it or doing a per-recipient lookup at write time buys nothing.
-- The panel's only join key to a QueryHub identity is the work email pushed
-- in Layer-A principal sync, so the read endpoint resolves slack_user_id ->
-- work email server-side before serving; a recipient with no resolvable
-- email is omitted from the served list and counted, not silently dropped.
-- The stored value and the served value are deliberately different shapes —
-- see the column comment below.

CREATE TABLE IF NOT EXISTS notification_outbox (
    id            BIGSERIAL   PRIMARY KEY,
    event_type    TEXT        NOT NULL,  -- queryhub.request_pending, queryhub.request_decided, ...
    request_id    BIGINT,                -- NULL for an event with no linked request
    recipients    TEXT[]      NOT NULL,  -- admins.list_active() slack_user_ids — see note above
    payload       JSONB       NOT NULL,  -- the {{vars}} the panel's template needs
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at  TIMESTAMPTZ,           -- NULL = pending
    attempts      INT         NOT NULL DEFAULT 0,
    last_error    TEXT
);

-- The poller's only query is "unprocessed, oldest first"; a full index on a
-- table that only grows would be paid for on every insert for no benefit.
-- Trailing `id` breaks ties deterministically: created_at is DEFAULT NOW(),
-- which is transaction-start time in Postgres, so two rows written inside
-- the same transaction tie exactly on created_at alone and "oldest first"
-- would be arbitrary between them. id (BIGSERIAL, strictly increasing) costs
-- nothing extra to add and makes the order well-defined. The panel's poller
-- must ORDER BY created_at, id to match.
CREATE INDEX IF NOT EXISTS idx_notification_outbox_pending
    ON notification_outbox (created_at, id)
    WHERE processed_at IS NULL;

COMMENT ON TABLE notification_outbox IS
$$One row per panel-facing notification event, written by application code at the point recipients are decided (e.g. core_submit.dispatch_and_notify for queryhub.request_pending). The IDP panel polls this as JSON, delivers in-app + Slack, and stamps processed_at. Distinct from the trigger-fed auth_event_outbox (migration 060), which has its own in-process poller.$$;

COMMENT ON COLUMN notification_outbox.recipients IS
$$QueryHub's own admins.list_active() slack_user_id values -- NOT email. The stored value and the value the panel is served differ on purpose: admins.list_active() carries no email, so widening it or doing a per-recipient lookup at write time would buy nothing. GET /admin/notifications/outbox resolves each slack_user_id to a work email server-side before serving, because the panel's only join key to a QueryHub identity is the work email pushed in Layer-A sync. A recipient with no resolvable email is omitted from the served list and counted, not silently dropped.$$;
