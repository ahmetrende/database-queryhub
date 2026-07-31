-- Migration 049: query_favorites — per-user "starred" queries
--
-- Lighter-weight than query_templates: a user stars a query they actually
-- ran (via a button on the result DM, or a checkbox in the /sql modal) and
-- it shows up in a personal favorites picker in the modal. No name is
-- required (unlike templates) — the query preview is the label by default.
-- Personal only; there is no shared/visibility concept here.

CREATE TABLE IF NOT EXISTS query_favorites (
    id                BIGSERIAL    PRIMARY KEY,
    slack_user_id     TEXT         NOT NULL
                      CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'),
    query             TEXT         NOT NULL,
    target_server_id  INT          REFERENCES target_servers(id) ON DELETE SET NULL,
    database_name     TEXT,
    label             TEXT,                      -- optional; UI falls back to a query preview
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_used_at      TIMESTAMPTZ,
    use_count         INTEGER      NOT NULL DEFAULT 0
);

-- Dedup: one favorite per (user, query text, target, db). target/db are
-- COALESCEd so NULLs don't defeat the uniqueness (NULL != NULL in a plain
-- UNIQUE). Starring the same thing twice just touches last_used_at.
CREATE UNIQUE INDEX IF NOT EXISTS uq_query_favorites_dedup
    ON query_favorites (
        slack_user_id,
        md5(query),
        COALESCE(target_server_id, -1),
        COALESCE(database_name, '')
    );

CREATE INDEX IF NOT EXISTS idx_query_favorites_owner
    ON query_favorites (slack_user_id, last_used_at DESC NULLS LAST);

COMMENT ON TABLE query_favorites IS
$$Per-user starred queries. Populated from the result-DM ⭐ button or the
/sql modal's "favorite this" checkbox; surfaced in a personal favorites
picker in the modal. Personal only (no sharing). Deduped per
(user, query, target, database).$$;
