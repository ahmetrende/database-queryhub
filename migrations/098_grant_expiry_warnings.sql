-- Warn before a standing grant lapses.
--
-- Migration 096 let a grant end on its own. Nothing announces it: the
-- auth-event triggers fire on row CHANGES, and time passing is not one, so
-- access simply stops working and the person finds out by being refused. The
-- offboarding that prompted 096 is the good case; the bad case is a grant that
-- was meant to be renewed and nobody was told.
--
-- Two warnings rather than one, because they answer different questions. A day
-- out is "arrange the renewal"; four hours out is "you are about to lose this
-- mid-task". Either alone leaves one of those unserved.
--
-- State lives here rather than on the grant rows: a warning is a property of
-- (grant, threshold), the grant tables should not grow a column per threshold,
-- and a grant whose expiry MOVES must be able to warn again — which it does,
-- because the recorded `expires_at` no longer matches and the row no longer
-- suppresses anything.
--
-- Auto-approve windows are deliberately NOT covered. They are short by design
-- and requested with a duration in mind; warning four hours ahead about a
-- one-hour window is noise, and losing one means going back to asking for
-- approval rather than losing access.

CREATE TABLE IF NOT EXISTS grant_expiry_notices (
    id             BIGSERIAL PRIMARY KEY,
    grant_kind     TEXT        NOT NULL CHECK (grant_kind IN ('user', 'team')),
    grant_id       BIGINT      NOT NULL,
    -- Hours before expiry this notice was for. Part of the identity: the
    -- 24-hour warning must not suppress the 4-hour one.
    threshold_hours INT        NOT NULL,
    -- The expiry the warning described. If someone extends the grant, this
    -- stops matching and the new deadline warns on its own.
    expires_at     TIMESTAMPTZ NOT NULL,
    notified_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    recipients     INT         NOT NULL DEFAULT 0,
    UNIQUE (grant_kind, grant_id, threshold_hours, expires_at)
);

COMMENT ON TABLE grant_expiry_notices IS
    'One row per (grant, threshold, deadline) already warned about. Keyed on '
    'expires_at so extending a grant re-arms its warnings.';

CREATE INDEX IF NOT EXISTS grant_expiry_notices_lookup
    ON grant_expiry_notices (grant_kind, grant_id);

-- `user_target_grants` has no surrogate key — its PK is
-- (slack_user_id, target_server_id) — so give it one to reference. Team grants
-- are the same shape.
ALTER TABLE user_target_grants ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE team_target_grants ADD COLUMN IF NOT EXISTS id BIGSERIAL;

INSERT INTO bot_config (key, value, description) VALUES
('grant_expiry_warn_enabled', 'on',
 'Warn a grant holder before a time-bounded grant lapses. Off leaves expiry '
 'silent, which is how it behaved before migration 098.'),
('grant_expiry_warn_hours', '24,4',
 'Comma-separated hours-before-expiry at which to warn. Each fires once per '
 'grant per deadline; extending a grant re-arms them. Empty disables warning '
 'without turning the feature off.')
ON CONFLICT (key) DO NOTHING;
