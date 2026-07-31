-- Universal authorization-change notifications: DB-level outbox.
--
-- Problem: the app DMs the affected user on SOME authorization changes
-- (/sql grant, RO-window approval, stale-grant reaper), but changes made
-- outside those code paths — operator SQL in psql, seed scripts, future
-- tools — notify nobody. Authorization must never change silently.
--
-- Design: AFTER triggers on every person-affecting authorization table
-- write one row per change into auth_event_outbox. A small poller thread
-- inside the bot turns unprocessed rows into Slack DMs (auth_events.py).
-- Because capture happens at the database, EVERY writer is covered —
-- including manual SQL.
--
-- Double-DM avoidance: app paths that already send their own (richer)
-- DM set a transaction-local GUC before writing:
--     SET LOCAL app.auth_dm_suppress = 'on';
-- and the trigger skips capture for that transaction.
--
-- Delivery is at-least-once: the poller marks a row processed after the
-- send attempt batch; a crash between send and mark can re-DM once.

CREATE TABLE IF NOT EXISTS auth_event_outbox (
    id            BIGSERIAL   PRIMARY KEY,
    table_name    TEXT        NOT NULL,
    op            TEXT        NOT NULL CHECK (op IN ('INSERT','UPDATE','DELETE')),
    slack_user_id TEXT,             -- set when the row targets one user
    team_id       INT,              -- set for team-scoped tables (fan-out in poller)
    old_row       JSONB,            -- NULL on INSERT
    new_row       JSONB,            -- NULL on DELETE
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at  TIMESTAMPTZ,      -- NULL = pending
    attempts      INT         NOT NULL DEFAULT 0,
    last_error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_event_outbox_pending
    ON auth_event_outbox (id)
    WHERE processed_at IS NULL;

COMMENT ON TABLE auth_event_outbox IS
$$One row per authorization change captured by trigger, regardless of the writer (app code, operator psql, scripts). The bot's auth_events poller DMs the affected user(s) and stamps processed_at. Rows with a transaction that set app.auth_dm_suppress='on' (app paths that DM on their own) are not captured.$$;

CREATE OR REPLACE FUNCTION auth_event_capture() RETURNS trigger AS $$
DECLARE
    v_old  JSONB;
    v_new  JSONB;
    v_user TEXT;
    v_team INT;
BEGIN
    -- App paths that already notify suppress capture for their txn.
    IF COALESCE(current_setting('app.auth_dm_suppress', true), '') IN ('on','1','true') THEN
        RETURN NULL;
    END IF;

    IF TG_OP <> 'INSERT' THEN v_old := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN v_new := to_jsonb(NEW); END IF;

    -- No-op UPDATEs (ON CONFLICT DO UPDATE with identical values, idempotent
    -- re-runs) carry no information — skip them at the source.
    IF TG_OP = 'UPDATE' AND v_old = v_new THEN
        RETURN NULL;
    END IF;

    v_user := COALESCE(v_new ->> 'slack_user_id', v_old ->> 'slack_user_id');
    v_team := COALESCE((v_new ->> 'team_id')::int, (v_old ->> 'team_id')::int);

    INSERT INTO auth_event_outbox (table_name, op, slack_user_id, team_id, old_row, new_row)
    VALUES (TG_TABLE_NAME, TG_OP, v_user, v_team, v_old, v_new);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Per-user authorization tables ---------------------------------------------

DROP TRIGGER IF EXISTS trg_auth_event ON user_target_grants;
CREATE TRIGGER trg_auth_event
    AFTER INSERT OR UPDATE OR DELETE ON user_target_grants
    FOR EACH ROW EXECUTE FUNCTION auth_event_capture();

DROP TRIGGER IF EXISTS trg_auth_event ON auto_approve_grants;
CREATE TRIGGER trg_auth_event
    AFTER INSERT OR UPDATE OR DELETE ON auto_approve_grants
    FOR EACH ROW EXECUTE FUNCTION auth_event_capture();

-- requesters: only the whitelist flag is an authorization change.
-- profile_sync rewrites name/email/tz constantly — UPDATE OF keeps that out.
DROP TRIGGER IF EXISTS trg_auth_event ON requesters;
CREATE TRIGGER trg_auth_event
    AFTER INSERT OR UPDATE OF enabled OR DELETE ON requesters
    FOR EACH ROW EXECUTE FUNCTION auth_event_capture();

DROP TRIGGER IF EXISTS trg_auth_event ON admins;
CREATE TRIGGER trg_auth_event
    AFTER INSERT OR UPDATE OF enabled, can_grant, max_tier, scope_team_ids, scope_target_ids
       OR DELETE ON admins
    FOR EACH ROW EXECUTE FUNCTION auth_event_capture();

DROP TRIGGER IF EXISTS trg_auth_event ON temp_admin_grants;
CREATE TRIGGER trg_auth_event
    AFTER INSERT OR UPDATE OR DELETE ON temp_admin_grants
    FOR EACH ROW EXECUTE FUNCTION auth_event_capture();

DROP TRIGGER IF EXISTS trg_auth_event ON user_row_limit_overrides;
CREATE TRIGGER trg_auth_event
    AFTER INSERT OR UPDATE OR DELETE ON user_row_limit_overrides
    FOR EACH ROW EXECUTE FUNCTION auth_event_capture();

-- Team-scoped authorization tables (poller fans out to members) -------------

DROP TRIGGER IF EXISTS trg_auth_event ON team_target_grants;
CREATE TRIGGER trg_auth_event
    AFTER INSERT OR UPDATE OR DELETE ON team_target_grants
    FOR EACH ROW EXECUTE FUNCTION auth_event_capture();

DROP TRIGGER IF EXISTS trg_auth_event ON team_members;
CREATE TRIGGER trg_auth_event
    AFTER INSERT OR DELETE ON team_members
    FOR EACH ROW EXECUTE FUNCTION auth_event_capture();
