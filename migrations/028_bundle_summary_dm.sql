-- Idempotency anchor for the bundle "summary DM" the requester gets
-- once every item in a bundle is decided + executed. The bot sets these
-- columns when the summary lands; subsequent state changes that re-fire
-- the aggregator (e.g. admin marks an awaiting_dba_manual item complete
-- a few hours later) see the message_ts and `chat.update` the existing
-- summary instead of posting a duplicate.

ALTER TABLE request_bundles
    ADD COLUMN IF NOT EXISTS requester_summary_channel_id TEXT;
ALTER TABLE request_bundles
    ADD COLUMN IF NOT EXISTS requester_summary_message_ts TEXT;

COMMENT ON COLUMN request_bundles.requester_summary_message_ts IS
$$Slack ts of the bundle-summary DM the requester received when the bundle reached a terminal state. NULL = not sent yet. Set once, then re-targeted by chat.update for subsequent (rare) state changes after manual DBA closure.$$;
