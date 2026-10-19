-- A switch that closes QueryHub's own web sign-in to everyone but super-admins.
--
-- After the portal cutover, developers reach the web UI through the portal.
-- The portal sends a signed identity assertion, so it never uses this sign-in.
-- This key lets an operator close the browser sign-in and the sessions it
-- made, for every person who is not a super-admin. Slack is not affected.
--
-- The default is `off`. Nothing changes until an operator sets it to `on`.
-- A second run changes nothing, and an install that set the key by hand keeps
-- its value.

INSERT INTO bot_config (key, value, description) VALUES
    ('web_own_ui_super_admin_only', 'off',
     'on = only a super-admin can sign in to the QueryHub web UI with Slack, SSO or a local account, and open sessions of other people end at once. A signed portal request is not affected. Slack is not affected. off = every enabled requester can sign in.')
ON CONFLICT (key) DO NOTHING;
