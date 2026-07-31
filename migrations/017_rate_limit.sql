-- Per-user request rate limit. Caps the number of in-flight requests
-- (pending / changes_requested / approved / scheduled / executing) per
-- Slack user, so a single requester (or compromised account) can't fill
-- the queue. Admins are exempt. Editable live via bot_config.

INSERT INTO bot_config (key, value, description) VALUES
    ('max_open_requests_per_user', '5',
     'Max concurrent in-flight /sql requests per non-admin Slack user '
     '(states: pending, changes_requested, approved, scheduled, executing). '
     'Submission is rejected with a friendly modal error when the user is '
     'at this cap. Set to a very high number to disable the rate limit.')
ON CONFLICT (key) DO NOTHING;
