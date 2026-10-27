-- A list of people who may use QueryHub's own web sign-in while
-- `web_own_ui_super_admin_only` is on, beside the super-admins.
--
-- The value is a comma-separated list of the ids that a session carries: a
-- Slack user id, or `local:<username>` for a local account. An empty value
-- allows nobody beyond the super-admins. The list has no effect while the
-- switch is `off`. A second run changes nothing, and an install that set the
-- key by hand keeps its value.

INSERT INTO bot_config (key, value, description) VALUES
    ('web_own_ui_allowed_users', '',
     'Comma-separated ids (Slack user id, or local:<username>) that may use the QueryHub web UI sign-in while web_own_ui_super_admin_only is on, beside the super-admins. Empty = only super-admins. No effect while the switch is off.')
ON CONFLICT (key) DO NOTHING;
