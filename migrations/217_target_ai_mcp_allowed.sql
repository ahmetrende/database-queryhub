-- 217: turn the AI assistant and the MCP door off for one connection.
--
-- Both features have a global switch only. Some servers must not reach either:
-- the AI assistant sends table names and table details to a model provider,
-- and an MCP client is a program that acts for a person. An admin can now
-- close one connection to each of them and keep it open to people.
--
-- ai_allowed false: the AI assistant refuses the connection before it reads
-- any table name (ai._granted).
-- mcp_allowed false: the MCP door does not list the connection, refuses every
-- tool on it, and refuses to read back a request on it (mcp_server/tools.py).
--
-- Both default to TRUE, which is the behaviour before this migration.

ALTER TABLE target_servers
    ADD COLUMN IF NOT EXISTS ai_allowed BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE target_servers
    ADD COLUMN IF NOT EXISTS mcp_allowed BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN target_servers.ai_allowed IS
    'The AI assistant may use this connection. When false, the assistant '
    'refuses it before it reads any table name (migration 217).';

COMMENT ON COLUMN target_servers.mcp_allowed IS
    'MCP clients may use this connection. When false, the MCP door does not '
    'list it and refuses every tool and every read-back on it (migration 217).';
