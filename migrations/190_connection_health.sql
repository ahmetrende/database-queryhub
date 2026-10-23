-- Connection health: whether QueryHub can reach each target, checked on a timer.
--
-- Before this, health showed only when an admin pressed "Test" on one
-- connection. A target with an expired certificate or a closed network path
-- looked fine on the Connections screen until a query failed on it.
--
-- One row per target, written by scripts/check_connection_health.py and
-- overwritten on each check. GET /admin/connections shows it as `health`.
-- The history that matters is the failure's start, `failing_since`, so this is
-- deliberately not a log: a table that grows per target per check is the kind
-- of thing nobody prunes.
--
-- `failing_since` is NULL while the target answers. The first failure after an
-- answer, or on the first check, sets it. Later failures keep it. The CHECK
-- holds that pairing, so a bug in the writer fails loudly here.
--
-- The rows are telemetry, as schema_refresh_health (migration 124) is: they
-- change no permission and no setting, so they carry no audit row.
--
-- A deleted target takes its row with it (ON DELETE CASCADE). The script
-- deletes the row of a target that it no longer checks (disabled, marked
-- deleted, or an excluded engine), and the API shows no row for such a
-- target, so an old result never appears beside it.

CREATE TABLE IF NOT EXISTS connection_health (
    target_server_id integer     PRIMARY KEY
                     REFERENCES target_servers(id) ON DELETE CASCADE,
    ok               boolean     NOT NULL,
    checked_at       timestamptz NOT NULL,
    failing_since    timestamptz,
    -- Scrubbed: no password, no login name, no connection string.
    error            text,
    latency_ms       integer,
    CONSTRAINT connection_health_failing_since
        CHECK ((ok AND failing_since IS NULL)
               OR (NOT ok AND failing_since IS NOT NULL)),
    CONSTRAINT connection_health_error
        CHECK ((ok AND error IS NULL)
               OR (NOT ok AND error IS NOT NULL AND char_length(error) <= 200)),
    CONSTRAINT connection_health_latency
        CHECK (latency_ms IS NULL OR latency_ms >= 0)
);

COMMENT ON TABLE connection_health IS
    'Last periodic connection check per target (scripts/check_connection_health.py). '
    'One row per target, overwritten on each check. failing_since is when the '
    'current run of failures started.';
