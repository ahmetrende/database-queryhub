-- QueryHub Web: server-side session state for the browser frontend.
--
-- The short-lived access JWT is stateless; what we persist is the
-- REFRESH token (opaque, hashed) — one row per browser login. Revoking
-- a row (or the whole user) kills the session at the next request,
-- because verify_session checks the revocation view on every call.
-- AUTH.md: refresh time is also where Slack users.info re-confirms the
-- human still exists in the workspace.

CREATE TABLE IF NOT EXISTS web_sessions (
    id                BIGSERIAL   PRIMARY KEY,
    slack_user_id     TEXT        NOT NULL
                      CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'),
    refresh_hash      TEXT        NOT NULL UNIQUE,   -- sha256 of the opaque refresh token
    auth_provider     TEXT        NOT NULL DEFAULT 'slack',
    user_agent        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_refresh_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at        TIMESTAMPTZ NOT NULL,          -- absolute refresh lifetime
    revoked_at        TIMESTAMPTZ,
    revoked_reason    TEXT
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_user
    ON web_sessions (slack_user_id, revoked_at);

COMMENT ON TABLE web_sessions IS
$$One row per QueryHub-Web browser login (refresh token, hashed). Access JWTs are short-lived + stateless; per-request revocation checks look for a live (non-revoked, non-expired) row matching the JWT's session id. Revoke a user instantly: UPDATE web_sessions SET revoked_at=NOW(), revoked_reason='…' WHERE slack_user_id='U…' AND revoked_at IS NULL.$$;
