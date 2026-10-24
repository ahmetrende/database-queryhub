-- How long an auto-approve rule made in the app may last.
--
-- A rule made from the web screens, the grant forms or the copy-access action
-- must have an end date. It cannot end later than this many days from now.
-- The application checks this (`auto_approve_limits.py`). The database does
-- not: there is no CHECK, no trigger and no NOT NULL on `expires_at` or on
-- `valid_until`. A person who inserts a row with SQL keeps a way to make a
-- rule with no end. That way is deliberate (operator decision, 2026-10-24).
-- OPERATIONS.md has the statement and the audit row to write with it.
--
-- The System configuration screen accepts a whole number from 1 to 365. A
-- value that is not a whole number in that range, set by hand, counts as 90.
-- Rules that exist now keep their end dates. A rule with no end stays as it is.
--
-- A second run changes nothing, and an install that set the key by hand keeps
-- its value.

INSERT INTO bot_config (key, value, description) VALUES
    ('auto_approve_max_days', '90',
     'Most days an auto-approve rule made in the app can last. Every such rule needs an end date, and the end cannot be later than this many days from now. A whole number from 1 to 365. A rule made with SQL can have no end. Rules that already exist keep their end dates.')
ON CONFLICT (key) DO NOTHING;
