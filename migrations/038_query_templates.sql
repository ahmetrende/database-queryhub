-- Saved queries (templates / favorites).
--
-- A user can save the current /sql submission as a named template
-- (`Save as template` field in the modal). Templates are personal
-- by default; flagging `is_shared = TRUE` makes them visible to
-- every workspace member.
--
-- Lookup pattern: `/sql template <name>` finds the most-specific
-- match (the user's own template before a shared one), opens the
-- modal pre-filled.

CREATE TABLE IF NOT EXISTS query_templates (
    id                BIGSERIAL    PRIMARY KEY,
    name              TEXT         NOT NULL,
    description       TEXT,
    query             TEXT         NOT NULL,
    target_server_id  INT          REFERENCES target_servers(id) ON DELETE SET NULL,
    database_name     TEXT,
    owner_slack_id    TEXT         NOT NULL
                      CHECK (owner_slack_id ~ '^[UW][A-Z0-9]{8,}$'),
    is_shared         BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_used_at      TIMESTAMPTZ,
    use_count         INTEGER      NOT NULL DEFAULT 0,
    CHECK (length(name) > 0 AND length(name) <= 64)
);

-- One template name per owner (case-insensitive). Lets the user
-- "Save as / overwrite" by re-saving with the same name; the submit
-- handler does ON CONFLICT DO UPDATE on this index.
CREATE UNIQUE INDEX IF NOT EXISTS uq_query_templates_owner_name
    ON query_templates (owner_slack_id, lower(name));

CREATE INDEX IF NOT EXISTS idx_query_templates_owner
    ON query_templates (owner_slack_id);

CREATE INDEX IF NOT EXISTS idx_query_templates_shared
    ON query_templates (is_shared) WHERE is_shared = TRUE;

COMMENT ON TABLE query_templates IS
$$User-saved /sql submissions. Personal by default (visible only to the owner); is_shared = TRUE promotes to all-workspace-visible. The /sql templates subcommand lists owner + shared rows; /sql template <name> opens the bot modal pre-filled from the most-specific match.$$;

COMMENT ON COLUMN query_templates.is_shared IS
$$FALSE = personal (only `owner_slack_id` sees it in /sql templates). TRUE = visible to everyone via /sql templates. Useful for "standard daily report" templates a team curates.$$;

COMMENT ON COLUMN query_templates.use_count IS
$$Incremented on every /sql template <name> resolution. Together with last_used_at, lets the owner spot abandoned templates.$$;
