-- A sync that would disable many requesters waits for a super-admin.
--
-- The IDP panel sends the full list of people who may use QueryHub every fifteen
-- minutes, and the reconcile disables whoever is missing. A wrong list (a role
-- not yet granted, a role removed by mistake) would lock that many people out of
-- Slack, the web and MCP within one tick. A run that would disable more than
-- idp_sync_max_disable requesters is held instead: nothing changes, the panel's
-- job gets a 409, and every super-admin gets one Slack card asking them to
-- approve it.
--
--   idp_sync_hold  One row per distinct list that was held. would_disable is the
--                  sorted set of Slack ids the run would have disabled. An
--                  approval covers those people, once, for 24 hours: the next
--                  run whose disable list lies inside the set is applied and
--                  stamps applied_at. cards remembers where each super-admin's
--                  card was posted, so one decision can close the others.
--
-- Inert while idp_assertion_enabled is off: only the sync principal reaches the
-- reconcile, and only through an assertion.

CREATE TABLE IF NOT EXISTS idp_sync_hold (
    id                  BIGSERIAL PRIMARY KEY,
    would_disable       TEXT[] NOT NULL CHECK (cardinality(would_disable) > 0),
    limit_at_hold       INT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'approved', 'rejected', 'superseded')),
    first_held_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_held_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_notified_at    TIMESTAMPTZ,
    cards               JSONB NOT NULL DEFAULT '[]'::jsonb,
    decided_by_slack_id TEXT,
    decided_by_name     TEXT,
    decided_at          TIMESTAMPTZ,
    applied_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_idp_sync_hold_status
    ON idp_sync_hold (status, last_held_at DESC);

INSERT INTO bot_config (key, value, description) VALUES
  ('idp_sync_max_disable', '5',
   'Most requesters one IDP sync may disable before a super-admin must approve it.')
ON CONFLICT (key) DO NOTHING;
