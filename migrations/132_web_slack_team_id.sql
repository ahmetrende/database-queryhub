-- 132: pin the Slack workspace a web sign-in must come from.
--
-- The Slack OIDC sign-in compares the id_token's team against the bot's own
-- workspace, discovered with auth.test. When auth.test failed, the check was
-- skipped for that sign-in: an account from any workspace got past the gate.
-- The gate now refuses a sign-in whenever the workspace cannot be established.
--
-- An install that runs the web UI with Slack sign-in but without the bot has
-- no bot token to call auth.test with, so it needs a way to say which
-- workspace it trusts. This key is that. Empty keeps the old source of truth,
-- auth.test with the bot token.
INSERT INTO bot_config (key, value, description) VALUES
  ('web_slack_team_id', '',
   'Slack workspace (team id, T...) a web sign-in must come from. Empty = the '
   'bot''s own workspace, discovered via auth.test. A sign-in is refused when '
   'neither can be established.')
ON CONFLICT (key) DO NOTHING;
