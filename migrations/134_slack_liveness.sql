-- 134: remember when Slack last said a person still works here.
--
-- The web app asks Slack (users.info) at sign-in, at every refresh and before
-- every RW/DDL submit. A transport error used to count as "still employed",
-- so while Slack could not be reached an offboarded person whose rows had not
-- been removed yet kept their session for as long as the outage lasted.
--
-- Now a write needs a live answer, and a sign-in or refresh passes on a
-- failed call only if Slack called the person active within
-- `web_employment_grace_hours`. This table is where that last answer lives:
-- one row per principal, rewritten on each "active" answer.
CREATE TABLE IF NOT EXISTS slack_liveness (
    principal_id TEXT PRIMARY KEY,
    active_at    TIMESTAMPTZ NOT NULL
);

INSERT INTO bot_config (key, value, description) VALUES
  ('web_employment_grace_hours', '2',
   'When Slack cannot be reached, a web sign-in or refresh still passes if '
   'Slack called the person active within this many hours. An RW/DDL submit '
   'always needs a live answer.')
ON CONFLICT (key) DO NOTHING;
