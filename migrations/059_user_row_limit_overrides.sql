-- Per-user result row-limit overrides (time-bounded).
--
-- Most users are fine with the global bot_config.max_rows cap, but some
-- legitimately need far more for exports. Rather than a permanent
-- escalation that gets forgotten, an admin grants a higher cap for a
-- period: `expires_at` makes it auto-lapse (resolution filters expired
-- rows), so nobody keeps an oversized cap indefinitely. One row per user
-- (upsert to change); expires_at NULL is allowed but discouraged.

CREATE TABLE IF NOT EXISTS user_row_limit_overrides (
    slack_user_id text PRIMARY KEY,
    max_rows      integer NOT NULL CHECK (max_rows > 0),
    expires_at    timestamptz,
    reason        text,
    granted_by    text,
    granted_at    timestamptz NOT NULL DEFAULT now()
);
