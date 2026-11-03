-- The switches of the MCP door, seeded with the safe values.
--
-- The portal (the IDP panel) calls the MCP door for a person who connected
-- Claude Code to the portal. `mcp_door_enabled` closes the door. It is a new
-- key, off by default: `mcp_enabled` switched the old stdio server, and an
-- install where that key is `on` must not get an open door from a deploy.
-- `mcp_max_tier` is the highest tier a program may submit through it.
-- `mcp_rate_limit_per_minute` caps the tool calls of one person.
-- `mcp_portal_url` is the address of QueryHub in the portal, for the link to a
-- request. The app reads every key on every call, so a change needs no
-- restart. An install that set a key by hand keeps its value, and a second run
-- changes nothing.

INSERT INTO bot_config (key, value, description) VALUES
    ('mcp_door_enabled', 'off',
     'on = the MCP door accepts tool calls from the portal. off = the MCP door refuses every tool call.'),
    ('mcp_max_tier', 'ro',
     'The highest tier a program may submit through the MCP door: ro, rw or ddl. An unknown value reads as ro. RW and DDL requests from MCP always need a person to approve them.'),
    ('mcp_rate_limit_per_minute', '60',
     'How many MCP tool calls one person may make in one minute. More calls get a rate_limited answer.'),
    ('mcp_portal_url', '',
     'The address of QueryHub in the portal, for example https://portal.example.com/queryhub. A request link is <address>?request=<id>. Empty = no request link.')
ON CONFLICT (key) DO NOTHING;
