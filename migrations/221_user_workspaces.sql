-- 221: keep a person's workspace and UI preferences on the server.
--
-- The web editor kept its open tabs, their SQL and the UI preferences in
-- browser storage only. A second browser or a cleared profile started empty.
-- This table keeps one copy per person and kind on the server
-- (web/routes_workspace.py).
--
-- kind 'workspace': the open tabs and their state.
-- kind 'prefs': the UI preferences.
--
-- version goes up by one on each write. A write names the version it read,
-- and the server refuses it with 409 when the stored version is different.
-- So two browsers cannot silently overwrite each other.
--
-- This is the person's own editor state, like browser storage. No audit row
-- is written for it.

CREATE TABLE IF NOT EXISTS user_workspaces (
    principal_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('workspace', 'prefs')),
    data jsonb NOT NULL,
    version integer NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, kind)
);

COMMENT ON TABLE user_workspaces IS
    'One person''s web workspace (open tabs) or UI preferences, kept on the '
    'server so that they follow the person to another browser. version goes up '
    'by one on each write. A write with an old version gets 409 (migration 221).';

INSERT INTO bot_config (key, value, description) VALUES
    ('workspace_sync_enabled', 'on',
     'Keep each person''s web workspace and UI preferences on the server. off = the routes answer 404 and the browser keeps them locally only.'),
    ('workspace_max_kb', '512',
     'Largest workspace or preferences document, in KB of JSON text, that one person can store. A larger write gets 413.')
ON CONFLICT (key) DO NOTHING;
