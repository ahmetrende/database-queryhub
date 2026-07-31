-- 052: auto-approve window requests
--
-- Backs the RO-burst nudge: a user who is running many reads can request a
-- short, narrow auto-approve window (default RO, 1h, scoped to one target).
-- The request carries a mandatory justification and is decided by an admin;
-- on approval the bot creates a target-scoped auto_approve_grants row and
-- links it here.
CREATE TABLE IF NOT EXISTS auto_approve_requests (
    id                  BIGSERIAL PRIMARY KEY,
    requester_slack_id  TEXT NOT NULL CHECK (requester_slack_id ~ '^[UW][A-Z0-9]{8,}$'),
    requester_name      TEXT,
    target_server_id    INT  REFERENCES target_servers(id) ON DELETE CASCADE,
    database_name       TEXT,
    max_tier            TEXT NOT NULL DEFAULT 'ro' CHECK (max_tier IN ('ro','rw','ddl')),
    window_minutes      INT  NOT NULL CHECK (window_minutes > 0),
    reason              TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','approved','rejected')),
    decided_by_slack_id TEXT,
    decided_by_name     TEXT,
    decided_at          TIMESTAMPTZ,
    granted_id          BIGINT REFERENCES auto_approve_grants(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Only one pending window request per (user, target) at a time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_auto_approve_requests_pending
    ON auto_approve_requests (requester_slack_id, target_server_id)
    WHERE status = 'pending';

COMMENT ON TABLE auto_approve_requests IS
$$RO-burst nudge: user-requested short auto-approve windows (mandatory justification, admin-decided). On approval the bot inserts a target-scoped auto_approve_grants row and stores its id in granted_id.$$;

-- Tunables for the RO-burst nudge (defaults also hard-coded in modal.py via
-- cfg.get_int, so absence is safe; seeded here so operators can see/tune them).
INSERT INTO bot_config (key, value, description) VALUES
    ('ro_burst_threshold', '3',  'RO-burst nudge: min RO requests in the window before the modal nudge shows.'),
    ('ro_burst_window_min', '10', 'RO-burst nudge: look-back window (minutes) for counting recent RO requests.'),
    ('ro_window_minutes', '60',  'RO-burst nudge: duration (minutes) of the auto-approve window granted on approval.')
ON CONFLICT (key) DO NOTHING;
