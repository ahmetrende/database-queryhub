-- Bot display overrides for chat.postMessage. Requires the chat:write.customize
-- bot scope (already granted on this app). Empty value disables the override
-- and the bot posts under its raw app name. Change via UPDATE bot_config —
-- runtime-effective, no restart needed.

INSERT INTO bot_config (key, value, description) VALUES
    ('bot_display_name', 'QueryHub',
     'Username override for chat.postMessage. Empty = use raw app name.'),
    ('bot_display_icon', ':query_hub:',
     'Icon emoji override for chat.postMessage (e.g. :robot_face:). Empty = use app icon.')
ON CONFLICT (key) DO NOTHING;
