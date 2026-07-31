-- 065_request_origin.sql
-- Record which channel a request came from so the executor can deliver the
-- result back to that same channel. Web-origin requests read their result in
-- the web UI, so by default the executor does NOT also DM the CSV to the
-- requester in Slack (bot_config `web_result_to_slack` flips that back on).
-- Existing rows default to 'slack' (the historical channel). Metadata-only
-- ADD COLUMN (DEFAULT is not table-rewriting on modern Postgres).

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'slack';
