-- How long a grant lasts when an access-request approval gives it no other end.
--
-- Approving an access request writes a per-user grant. Until now that grant had
-- no end date. From now it ends in this many days, unless the approver picks
-- an end or picks no end. The Slack Approve button always takes the default.
-- The application applies this (`access_grant_default.py`). A grant that an
-- admin gives from the grant forms is not touched: it keeps the end the admin
-- picked, and "no expiry" stays a choice. There is no upper limit on a chosen
-- end (operator decision, 2026-10-24).
--
-- The System configuration screen accepts a whole number from 1 to 365. A
-- value that is not a whole number in that range, set by hand, counts as 90.
-- Grants that exist now keep their end dates. A grant with no end stays as it
-- is. An approval that merges into an active grant of the same tier keeps that
-- grant's end date.
--
-- A second run changes nothing, and an install that set the key by hand keeps
-- its value.

INSERT INTO bot_config (key, value, description) VALUES
    ('access_grant_default_days', '90',
     'Days that a new grant lasts when an approver approves an access request and picks no other end. A whole number from 1 to 365. The approver can pick another end or no end. Grants that already exist keep their end dates.')
ON CONFLICT (key) DO NOTHING;
