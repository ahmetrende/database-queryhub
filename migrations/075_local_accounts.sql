-- Local (built-in) accounts for the vanilla profile — login without Slack
-- or any external IdP.
--
-- The canonical identity everywhere in QueryHub is ONE text column
-- (historically named slack_user_id). It is not really "a Slack id" — it is
-- "the principal id", and grants, admins, teams, audit, favorites, ratings
-- and auto-approve all key on it by plain string equality. A local account
-- simply uses a namespaced value in that SAME column:
--
--     local:<username>        (this migration)
--     U0EXAMPLE01             (Slack member id, unchanged)
--     google:<email>          (reserved for a later version)
--
-- So a local user flows through the whole authorization model unchanged; it
-- is a distinct principal, NOT linked to any Slack id (no cross-provider
-- identity merge in this version).
--
-- To make that work we widen the id-format CHECK on every table that stores a
-- principal id, to also accept the `local:<username>` form. The new pattern is
-- a strict SUPERSET of the old one (every existing Slack id still matches the
-- first alternative), so ADD CONSTRAINT re-validates all existing rows without
-- rejecting any. DROP ... IF EXISTS keeps the migration idempotent.

-- ---------------------------------------------------------------------------
-- 1) Widen the principal-id CHECK on all identity-bearing tables.
-- ---------------------------------------------------------------------------
-- Slack ids: ^[UW][A-Z0-9]{8,}$   |   local ids: ^local:<username>$
-- Username charset mirrors the local_users CHECK below (lowercase, starts
-- alnum, then [a-z0-9._-], total <= 60).

ALTER TABLE requesters DROP CONSTRAINT IF EXISTS requesters_slack_user_id_check;
ALTER TABLE requesters ADD  CONSTRAINT requesters_slack_user_id_check
    CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'
        OR slack_user_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$');

ALTER TABLE team_members DROP CONSTRAINT IF EXISTS team_members_slack_user_id_check;
ALTER TABLE team_members ADD  CONSTRAINT team_members_slack_user_id_check
    CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'
        OR slack_user_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$');

ALTER TABLE user_target_grants DROP CONSTRAINT IF EXISTS user_target_grants_slack_user_id_check;
ALTER TABLE user_target_grants ADD  CONSTRAINT user_target_grants_slack_user_id_check
    CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'
        OR slack_user_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$');

ALTER TABLE temp_admin_grants DROP CONSTRAINT IF EXISTS temp_admin_grants_slack_user_id_check;
ALTER TABLE temp_admin_grants ADD  CONSTRAINT temp_admin_grants_slack_user_id_check
    CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'
        OR slack_user_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$');

ALTER TABLE auto_approve_grants DROP CONSTRAINT IF EXISTS auto_approve_grants_slack_user_id_check;
ALTER TABLE auto_approve_grants ADD  CONSTRAINT auto_approve_grants_slack_user_id_check
    CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'
        OR slack_user_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$');

ALTER TABLE auto_approve_requests DROP CONSTRAINT IF EXISTS auto_approve_requests_requester_slack_id_check;
ALTER TABLE auto_approve_requests ADD  CONSTRAINT auto_approve_requests_requester_slack_id_check
    CHECK (requester_slack_id ~ '^[UW][A-Z0-9]{8,}$'
        OR requester_slack_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$');

ALTER TABLE query_favorites DROP CONSTRAINT IF EXISTS query_favorites_slack_user_id_check;
ALTER TABLE query_favorites ADD  CONSTRAINT query_favorites_slack_user_id_check
    CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'
        OR slack_user_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$');

ALTER TABLE query_templates DROP CONSTRAINT IF EXISTS query_templates_owner_slack_id_check;
ALTER TABLE query_templates ADD  CONSTRAINT query_templates_owner_slack_id_check
    CHECK (owner_slack_id ~ '^[UW][A-Z0-9]{8,}$'
        OR owner_slack_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$');

ALTER TABLE report_excluded_users DROP CONSTRAINT IF EXISTS report_excluded_users_slack_user_id_check;
ALTER TABLE report_excluded_users ADD  CONSTRAINT report_excluded_users_slack_user_id_check
    CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'
        OR slack_user_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$');

ALTER TABLE web_sessions DROP CONSTRAINT IF EXISTS web_sessions_slack_user_id_check;
ALTER TABLE web_sessions ADD  CONSTRAINT web_sessions_slack_user_id_check
    CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'
        OR slack_user_id ~ '^local:[a-z0-9][a-z0-9._-]{0,59}$');

-- ---------------------------------------------------------------------------
-- 2) local_users — credentials only. Authorization still lives in requesters
--    / admins (keyed on the local:<username> principal id), exactly like a
--    Slack user. Passwords are stored as a salted, iterated PBKDF2 hash
--    (see src/queryhub/passwords.py) — NEVER cleartext.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS local_users (
    username       TEXT PRIMARY KEY
        CHECK (username ~ '^[a-z0-9][a-z0-9._-]{0,59}$'),
    -- Format: pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>. Self-describing
    -- so the cost can be raised later without invalidating older hashes.
    password_hash  TEXT NOT NULL,
    display_name   TEXT,
    email          TEXT,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    -- Optional: force a password reset at next login (e.g. after a bootstrap
    -- account hand-off). Not enforced by the API yet; reserved.
    must_change_pw BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by     TEXT,
    last_login_at  TIMESTAMPTZ
);

COMMENT ON TABLE local_users IS
$$Built-in (non-SSO) login accounts for the vanilla profile. Credentials only — authorization is in requesters/admins keyed on the principal id local:<username>. password_hash is a salted PBKDF2-HMAC-SHA256 digest (pbkdf2_sha256$iters$salt$hash); cleartext passwords are never stored.$$;
COMMENT ON COLUMN local_users.password_hash IS 'Salted, iterated PBKDF2-HMAC-SHA256 digest — never cleartext.';

-- ---------------------------------------------------------------------------
-- 3) Login toggle. Default OFF: an existing Slack-authenticated deployment is
--    unaffected; the vanilla profile turns it on (the bootstrap CLI does this
--    automatically when it creates the first local account).
-- ---------------------------------------------------------------------------
INSERT INTO bot_config (key, value, description) VALUES
    ('web_auth_local_enabled', 'off',
     'Allow username/password login with built-in local_users accounts (vanilla profile). Default off — enable only when running without Slack SSO.')
ON CONFLICT (key) DO NOTHING;
