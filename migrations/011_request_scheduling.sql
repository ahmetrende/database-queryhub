-- Scheduling support: a /sql submission can carry a `scheduled_for` time;
-- on approval the request enters status='scheduled' and a daemon thread in
-- the bot picks it up when due. NULL scheduled_for keeps the existing
-- "approve = run immediately" behaviour.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ;

COMMENT ON COLUMN requests.scheduled_for IS
$$When the user wants this query to run. NULL = run immediately on approval. If set, the approval moves status to 'scheduled' and the bot's scheduler thread dispatches it once NOW() reaches scheduled_for. Window capped by bot_config.max_schedule_days.$$;

-- Speeds up the scheduler poll: pick the next due rows in scheduled_for order.
CREATE INDEX IF NOT EXISTS idx_requests_scheduled_due
    ON requests (scheduled_for)
    WHERE status = 'scheduled';

INSERT INTO bot_config (key, value, description) VALUES
    ('max_schedule_days', '7',
     'Maximum days into the future a /sql query can be scheduled. Set to 0 to disable scheduling entirely (modal then rejects any non-empty schedule input).')
ON CONFLICT (key) DO NOTHING;

-- Coordinates of the requester-side DM that carries the [Cancel] button for
-- a scheduled request. Set when the scheduled DM is sent, NULL'd / used by
-- chat.update when the request is cancelled or starts executing.
ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS requester_dm_channel_id TEXT;
ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS requester_dm_message_ts TEXT;
