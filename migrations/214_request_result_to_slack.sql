-- A requester can ask for the result in Slack too, per request.
-- Only a non-Slack origin needs it. A Slack request always gets its result
-- in Slack. The global key web_result_to_slack stays as the default.
ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS result_to_slack boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN requests.result_to_slack IS
    'Requester chose to also receive the result in Slack (non-Slack origin).';
