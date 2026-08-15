-- CSV bulk import via COPY.
--
-- Users with an import grant upload a CSV through `/sql import` and the
-- bot bulk-loads it (COPY FROM STDIN) into the `dba` schema — either a
-- new auto-generated table (all TEXT columns) or an existing dba.* table.
-- Admin approval is always required; the load runs with the target's DDL
-- credentials (queryhub_ddl) since a new table needs CREATE.
--
-- Hard invariant: schema is ALWAYS 'dba'. The feature can never touch a
-- prod schema — its blast radius is the dba staging schema only.

-- Who may use the import feature (bot-level permission, per user, all
-- targets). Separate from query grants: holding RW/DDL does not imply
-- import rights, and vice versa.
CREATE TABLE IF NOT EXISTS import_grants (
    slack_user_id  TEXT PRIMARY KEY,
    granted_by     TEXT,
    granted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason         TEXT
);

COMMENT ON TABLE import_grants IS
$$Bot-level allowlist for the CSV import feature. A row here lets the
user run /sql import on any target they can otherwise reach. The COPY
itself uses the target's DDL credentials.$$;

-- One row per import request. Mirrors the shape of `requests` but for
-- the COPY pipeline (no SQL text; a CSV file + target table instead).
CREATE TABLE IF NOT EXISTS csv_imports (
    id                      BIGSERIAL PRIMARY KEY,
    requester_slack_id      TEXT NOT NULL,
    requester_name          TEXT,
    target_server_id        INTEGER NOT NULL,
    database_name           TEXT NOT NULL,
    table_name              TEXT NOT NULL,          -- unqualified; schema is always 'dba'
    is_new_table            BOOLEAN NOT NULL,
    unlogged                BOOLEAN NOT NULL DEFAULT TRUE,   -- new-table only
    delimiter               TEXT NOT NULL DEFAULT ',',
    columns                 JSONB,                  -- parsed CSV header (normalized)
    row_count               INTEGER,                -- CSV data rows (excl. header)
    byte_size               BIGINT,
    csv_file_path           TEXT,                   -- local downloaded copy
    slack_file_id           TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','executing',
                          'completed','failed','rejected')),
    decided_by_slack_id     TEXT,
    decided_by_name         TEXT,
    decided_at              TIMESTAMPTZ,
    decision_reason         TEXT,
    inserted_rows           INTEGER,                -- actual rows COPYed
    error_message           TEXT,
    requester_dm_channel_id TEXT,
    requester_dm_message_ts TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at             TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_csv_imports_status
    ON csv_imports (status, created_at);

COMMENT ON TABLE csv_imports IS
$$One row per /sql import request: a CSV bulk-loaded via COPY into a
dba.* table (new or existing). Admin-approved; runs with DDL creds.$$;

-- Admin DM fan-out tracking for imports (parallel to request_notifications).
CREATE TABLE IF NOT EXISTS import_notifications (
    id              BIGSERIAL PRIMARY KEY,
    import_id       BIGINT NOT NULL REFERENCES csv_imports(id) ON DELETE CASCADE,
    admin_slack_id  TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    message_ts      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_import_notifications_import
    ON import_notifications (import_id);

-- Config. Feature ships OFF; flip on after smoke test.
INSERT INTO bot_config (key, value, description) VALUES
    ('csv_import_enabled', 'off',
     'Enable the /sql import CSV bulk-load feature.'),
    ('import_max_rows', '100000',
     'Max CSV data rows accepted for a single import.'),
    ('import_max_mb', '50',
     'Max uploaded CSV size (MB) for a single import.'),
    ('import_csv_ttl_hours', '24',
     'Hours an uploaded import CSV is kept (local + Slack) before cleanup.'),
    ('import_timeout_sec', '600',
     'statement_timeout for the COPY load — higher than query_timeout_sec since bulk loads run longer.')
ON CONFLICT (key) DO NOTHING;
