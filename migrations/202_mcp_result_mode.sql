-- What a program may read of a result through the MCP door.
--
-- `metadata_only` (the default) sends the status, the row count and the column
-- names. It sends no value of the result. The person reads the result in the
-- QueryHub web UI. `masked_rows` also sends the masked rows. Those rows then
-- reach the model. Read on every call, so a change needs no restart. An unknown
-- value reads as `metadata_only`.
--
-- A second run changes nothing, and an install that set the key by hand keeps
-- its value.

INSERT INTO bot_config (key, value, description) VALUES
    ('mcp_result_mode', 'metadata_only',
     'What a program may read of a result through the MCP door. metadata_only = the status, the row count and the column names. No row. The person opens the result in the web UI. masked_rows = the masked rows too. An unknown value reads as metadata_only.')
ON CONFLICT (key) DO NOTHING;
