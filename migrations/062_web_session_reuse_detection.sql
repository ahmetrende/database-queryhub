-- Refresh-token reuse detection for QueryHub Web sessions.
--
-- Rotation (sessions.rotate_refresh) is single-use: each refresh swaps the
-- stored hash. We now remember the JUST-SUPERSEDED hash too, so if a token
-- that was already rotated away is presented again, we can tell it apart
-- from an unknown/expired token: replay of a superseded refresh token is a
-- strong theft signal (the legit client has moved on to the new token).
-- On detection we revoke the whole session (both the stolen old token and
-- the current one), forcing a clean re-login — the OAuth-BCP response to a
-- detected refresh-token replay. The client-side single-flight refresh
-- (qh-api.jsx) means a healthy browser never replays a superseded token,
-- so this fires only on genuine reuse.

ALTER TABLE web_sessions
    ADD COLUMN IF NOT EXISTS prev_refresh_hash TEXT;

-- Lookup path for the reuse check.
CREATE INDEX IF NOT EXISTS idx_web_sessions_prev_refresh
    ON web_sessions (prev_refresh_hash)
    WHERE prev_refresh_hash IS NOT NULL;

COMMENT ON COLUMN web_sessions.prev_refresh_hash IS
$$sha256 of the refresh token that was rotated away on the last refresh. If a presented refresh token matches this (not the current refresh_hash) on a still-live session, it is a replay of a superseded token -> treat as theft, revoke the session.$$;
