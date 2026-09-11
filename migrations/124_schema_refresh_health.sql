-- Per-target health for the hourly schema-catalog refresh.
--
-- The refresh logs a WARNING when a target fails and returns 0 either way, so
-- a target whose catalog stopped updating was invisible: the browse/search
-- screens and the /sql autocomplete kept serving the last good snapshot, and
-- nothing on any screen said it was old. A target can sit like that for days
-- -- an expired credential, a host that moved, a network path that closed --
-- and the first symptom is somebody asking why a table they just created is
-- not in the picker.
--
-- One row per target, written by `scripts/refresh_schema_catalog.py` after
-- each attempt. `alerted_at` is what keeps an hourly job from sending an
-- hourly DM: it is set when the streak crosses the threshold and cleared when
-- the target recovers, so each outage produces exactly one alert and one
-- all-clear.
--
-- Deliberately NOT a log: one row per target, overwritten. The history that
-- matters is already in `audit_log`, and a table that grows hourly per target
-- is the kind of thing nobody prunes.

CREATE TABLE IF NOT EXISTS schema_refresh_health (
    target_server_id     integer PRIMARY KEY
                         REFERENCES target_servers(id) ON DELETE CASCADE,
    last_ok_at           timestamptz,
    last_attempt_at      timestamptz NOT NULL DEFAULT now(),
    consecutive_failures integer     NOT NULL DEFAULT 0,
    last_error           text,
    -- Set when an alert has been sent for the CURRENT outage; NULL means no
    -- outage is being reported. Never a timestamp of the failure itself.
    alerted_at           timestamptz
);

COMMENT ON TABLE schema_refresh_health IS
    'Per-target outcome of the hourly schema-catalog refresh. One row per '
    'target, overwritten each run; alerted_at makes the alert once-per-outage.';
