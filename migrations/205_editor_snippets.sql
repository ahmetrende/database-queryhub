-- 205: editor snippets of a person (2026-11-11, Devart D3 and D4).
--
-- A snippet is a short key and a SQL body. In the web editor, the key + Tab
-- puts the body at the caret, as the built-in snippets do (ssf, cte). The
-- owner creates, edits, moves to a folder and deletes a snippet. A shared
-- snippet is visible to every QueryHub user, as a shared Slack template is
-- (query_templates.is_shared). Only the owner edits it. The owner or an
-- admin deletes it. `snippets.py` holds the same limits as these checks.
-- Safe to run again.

CREATE TABLE IF NOT EXISTS editor_snippets (
    id           BIGSERIAL    PRIMARY KEY,
    owner_id     TEXT         NOT NULL
                 CHECK (owner_id ~ '^[UW][A-Z0-9]{8,}$'
                     OR owner_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$'),
    key          TEXT         NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{1,31}$'),
    description  TEXT         CHECK (description IS NULL OR length(description) <= 200),
    body         TEXT         NOT NULL CHECK (length(body) BETWEEN 1 AND 20000),
    engine       TEXT         CHECK (engine IS NULL
                                     OR engine IN ('postgres', 'mssql', 'clickhouse', 'athena')),
    folder       TEXT         CHECK (folder IS NULL OR length(folder) BETWEEN 1 AND 64),
    is_shared    BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- One key per owner. Keys are lower case (the check above), so a plain index
-- is enough.
CREATE UNIQUE INDEX IF NOT EXISTS uq_editor_snippets_owner_key
    ON editor_snippets (owner_id, key);

CREATE INDEX IF NOT EXISTS idx_editor_snippets_shared
    ON editor_snippets (is_shared) WHERE is_shared;
