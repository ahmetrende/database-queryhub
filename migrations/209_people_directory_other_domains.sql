-- 209: hide people outside the company email domain in the person picker (2026-11-13).
--
-- The person picker finds people in the Slack directory by name or email.
-- The workspace also holds people outside the company domain
-- (web_allowed_email_domain). With 'hide' the picker leaves them out and
-- says how many it hid. With 'show' it lists them with a red "Outside" chip.
-- People already in QueryHub always show, flagged. Safe to run again.

INSERT INTO bot_config (key, value, description) VALUES
    ('people_directory_other_domains', 'hide',
     'hide | show. People whose email is outside web_allowed_email_domain: hide them in the person picker, or show them with a warning.')
ON CONFLICT (key) DO NOTHING;
