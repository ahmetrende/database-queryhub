-- 213: close the Slack entry (/sql and its modals) to all but a list (2026-11-17).
--
-- With 'on', only a super-admin or a person on slack_own_ui_allowed_users can
-- run /sql, submit a /sql modal or open a request again. Everyone else gets a
-- notice with a link to web_portal_url. Approval buttons, result messages and
-- ratings stay open. The app reads both keys on each use. Safe to run again.

INSERT INTO bot_config (key, value, description) VALUES
    ('slack_own_ui_super_admin_only', 'off',
     'on | off. When on, only super-admins and the people on slack_own_ui_allowed_users can use /sql in Slack. Others get a link to the portal.'),
    ('slack_own_ui_allowed_users', '',
     'Comma-separated Slack user ids that can use /sql while slack_own_ui_super_admin_only is on.')
ON CONFLICT (key) DO NOTHING;
