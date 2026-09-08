-- Keep the new tables current, whoever does the writing.
--
-- `access_model_v2` switches READS to the nine-table model, and every write
-- path still targets the old tables. While that is true the flag cannot safely
-- be turned on: a grant issued after the last copy would exist in one model
-- and not the other, and the person refused would have no way to tell why.
--
-- The obvious fix is to change the ~25 write sites. It is the wrong one. An
-- operator granting access in psql is a documented, used path — migration 060
-- exists precisely because app code is not the only writer — and no amount of
-- Python covers it. So the projection is maintained where every writer must
-- pass: AFTER triggers on the old tables, recomputing the affected subject.
--
-- RECOMPUTE, NOT TRANSLATE. Each trigger names a subject (a principal or a
-- team) and the function rebuilds every mirrored row for it from the old
-- tables. That is idempotent by construction, so a missed edge case shows up
-- as a row that gets corrected on the next write rather than as permanent
-- drift, and the same function serves the initial backfill and the ongoing
-- sync — one implementation of the mapping, not two.
--
-- The mirror is gated by `bot_config.access_model_mirror` (default on). If it
-- ever breaks it can be switched off without a deploy, at the cost of drift
-- that the copy script repairs.

ALTER TABLE access_grant
  ADD COLUMN IF NOT EXISTS mirrored_from TEXT;
ALTER TABLE role_assignment
  ADD COLUMN IF NOT EXISTS mirrored_from TEXT;
ALTER TABLE principal_setting
  ADD COLUMN IF NOT EXISTS mirrored_from TEXT;

COMMENT ON COLUMN access_grant.mirrored_from IS
  'The legacy table this row is a projection of, or NULL when it was written '
  'directly. The mirror owns exactly its own rows: it may revoke one it no '
  'longer finds a source for, and must never touch a row somebody wrote here.';

INSERT INTO bot_config (key, value, description) VALUES
  ('access_model_mirror', 'on',
   'Project writes on the legacy authorization tables into the nine-table '
   'model, so reads can be switched over without the two drifting.')
ON CONFLICT (key) DO NOTHING;

-- Backfill the marker for what the copy script already wrote, so the mirror
-- recognises those rows as its own instead of leaving them beside its work.
UPDATE access_grant SET mirrored_from = CASE
    WHEN reason = 'carried from team_target_grants' THEN 'team_target_grants'
    WHEN reason = 'carried from user_target_grants' THEN 'user_target_grants'
    WHEN reason = 'carried from bypass_team_grants' THEN 'requesters'
    ELSE 'auto_approve_grants' END
  WHERE mirrored_from IS NULL
    AND (reason LIKE 'carried from %' OR auto_approve);
UPDATE role_assignment SET mirrored_from = 'admins'
  WHERE mirrored_from IS NULL AND reason = 'carried from admins';
UPDATE principal_setting SET mirrored_from = CASE
    WHEN setting_key = 'max_rows' THEN 'user_row_limit_overrides'
    ELSE 'report_excluded_users' END
  WHERE mirrored_from IS NULL;

-- ---------------------------------------------------------------------------
-- do not notify twice
-- ---------------------------------------------------------------------------
--
-- The old table's own auth-event trigger already told the person what changed.
-- The mirror then writes the same fact into `access_grant`, whose trigger would
-- tell them again. Suppress capture on the NEW tables while mirroring — and
-- only those, so the old table's message still goes out. Naming the tables
-- rather than suppressing everything makes this independent of the order
-- Postgres happens to fire two triggers on one table in.
CREATE OR REPLACE FUNCTION auth_event_capture() RETURNS trigger AS $$
DECLARE
    v_old  JSONB;
    v_new  JSONB;
    v_user TEXT;
    v_team INT;
    v_pid  BIGINT;
BEGIN
    IF COALESCE(current_setting('app.auth_dm_suppress', true), '') IN ('on','1','true') THEN
        RETURN NULL;
    END IF;

    IF TG_TABLE_NAME IN ('access_grant', 'role_assignment', 'team_member',
                         'principal', 'principal_setting')
       AND COALESCE(current_setting('app.auth_mirror', true), '') = 'on' THEN
        RETURN NULL;
    END IF;

    IF TG_OP <> 'INSERT' THEN v_old := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN v_new := to_jsonb(NEW); END IF;
    IF TG_OP = 'UPDATE' AND v_old = v_new THEN
        RETURN NULL;
    END IF;

    v_user := COALESCE(v_new ->> 'slack_user_id', v_old ->> 'slack_user_id');
    v_team := COALESCE((v_new ->> 'team_id')::int, (v_old ->> 'team_id')::int);

    IF v_user IS NULL THEN
        v_pid := COALESCE((v_new ->> 'principal_id')::bigint,
                          (v_old ->> 'principal_id')::bigint,
                          CASE WHEN TG_TABLE_NAME = 'principal'
                               THEN COALESCE((v_new ->> 'id')::bigint,
                                             (v_old ->> 'id')::bigint) END);
        IF v_pid IS NOT NULL THEN
            SELECT i.external_id INTO v_user
              FROM principal_identity i
             WHERE i.principal_id = v_pid AND i.provider = 'slack'
               AND NOT i.is_deleted
             LIMIT 1;
        END IF;
    END IF;

    INSERT INTO auth_event_outbox (table_name, op, slack_user_id, team_id, old_row, new_row)
    VALUES (TG_TABLE_NAME, TG_OP, v_user, v_team, v_old, v_new);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- the mapping, once
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION mirror_enabled() RETURNS boolean AS $$
    SELECT COALESCE((SELECT lower(value) = 'on' FROM bot_config
                      WHERE key = 'access_model_mirror'), TRUE);
$$ LANGUAGE sql STABLE;

-- The principal row for a Slack id, created if this is the first sight of them.
CREATE OR REPLACE FUNCTION mirror_principal_id(p_slack TEXT) RETURNS BIGINT AS $$
DECLARE v_pid BIGINT;
BEGIN
    SELECT i.principal_id INTO v_pid FROM principal_identity i
     WHERE i.provider = 'slack' AND i.external_id = p_slack AND NOT i.is_deleted;
    IF v_pid IS NOT NULL THEN RETURN v_pid; END IF;

    INSERT INTO principal (kind, display_name, email, tz, enabled)
    SELECT 'person', COALESCE(r.name, a.name), COALESCE(r.email, a.email),
           COALESCE(r.tz, a.tz), COALESCE(r.enabled, a.enabled, FALSE)
      FROM (SELECT 1) one
      LEFT JOIN requesters r ON r.slack_user_id = p_slack
      LEFT JOIN admins     a ON a.slack_user_id = p_slack
    RETURNING id INTO v_pid;

    INSERT INTO principal_identity (principal_id, provider, external_id)
    VALUES (v_pid, 'slack', p_slack);
    RETURN v_pid;
END;
$$ LANGUAGE plpgsql;

-- Everything whose subject is one person.
CREATE OR REPLACE FUNCTION mirror_principal(p_slack TEXT) RETURNS void AS $$
DECLARE
    v_pid BIGINT;
BEGIN
    IF p_slack IS NULL OR NOT mirror_enabled() THEN RETURN; END IF;
    PERFORM set_config('app.auth_mirror', 'on', true);
    v_pid := mirror_principal_id(p_slack);

    UPDATE principal p SET
        display_name = COALESCE(r.name, a.name, p.display_name),
        email        = COALESCE(r.email, a.email, p.email),
        tz           = COALESCE(r.tz, a.tz, p.tz),
        enabled      = COALESCE(r.enabled, a.enabled, FALSE)
      FROM (SELECT 1) one
      LEFT JOIN requesters r ON r.slack_user_id = p_slack
      LEFT JOIN admins     a ON a.slack_user_id = p_slack
     WHERE p.id = v_pid;

    -- Access. One row per (target, database, tier, auto_approve); the source
    -- arrays are expanded, and a NULL array means every database.
    CREATE TEMP TABLE IF NOT EXISTS _want (
        target_id BIGINT, all_targets BOOL, database_name TEXT,
        all_databases BOOL, tier TEXT, auto_approve BOOL,
        merge_with_team BOOL, db_role TEXT, valid_from TIMESTAMPTZ,
        valid_until TIMESTAMPTZ, revoked_at TIMESTAMPTZ, src TEXT
    ) ON COMMIT DROP;
    DELETE FROM _want;

    INSERT INTO _want
    SELECT u.target_server_id, FALSE, d.db, d.db IS NULL, u.mode, FALSE, FALSE,
           NULL, u.granted_at, u.expires_at, u.revoked_at, 'user_target_grants'
      FROM user_target_grants u
      LEFT JOIN LATERAL unnest(COALESCE(u.allowed_databases, ARRAY[NULL::text]))
             AS d(db) ON TRUE
     WHERE u.slack_user_id = p_slack;

    INSERT INTO _want
    SELECT a.target_server_id, a.target_server_id IS NULL, a.database_name,
           a.database_name IS NULL, a.max_tier, TRUE, TRUE, NULL,
           a.starts_at, a.expires_at, NULL, 'auto_approve_grants'
      FROM auto_approve_grants a
     WHERE a.slack_user_id = p_slack
       AND (a.expires_at IS NULL OR a.expires_at > NOW());

    INSERT INTO _want
    SELECT NULL, TRUE, NULL, TRUE, 'ddl', FALSE, FALSE, NULL,
           NOW(), NULL, NULL, 'requesters'
      FROM requesters r
     WHERE r.slack_user_id = p_slack AND r.bypass_team_grants;

    -- Revoke what the mirror wrote and no longer has a source for. Revoked,
    -- not deleted: a grant that existed is a fact, and the person may be
    -- reading a message about it.
    UPDATE access_grant g SET revoked_at = NOW()
     WHERE g.principal_id = v_pid AND g.mirrored_from IS NOT NULL
       AND g.revoked_at IS NULL AND NOT g.is_deleted
       AND NOT EXISTS (
           SELECT 1 FROM _want w
            WHERE w.revoked_at IS NULL
              AND w.target_id IS NOT DISTINCT FROM g.target_id
              AND w.database_name IS NOT DISTINCT FROM g.database_name
              AND w.tier = g.tier AND w.auto_approve = g.auto_approve);

    INSERT INTO access_grant
        (principal_id, target_id, all_targets, database_name, all_databases,
         tier, auto_approve, merge_with_team, db_role, valid_from, valid_until,
         reason, mirrored_from)
    SELECT v_pid, w.target_id, w.all_targets, w.database_name, w.all_databases,
           w.tier, w.auto_approve, w.merge_with_team, w.db_role, w.valid_from,
           w.valid_until, 'mirrored from ' || w.src, w.src
      FROM (SELECT DISTINCT ON (target_id, database_name, tier, auto_approve) *
              FROM _want WHERE revoked_at IS NULL
             ORDER BY target_id, database_name, tier, auto_approve,
                      valid_until DESC NULLS FIRST) w
     WHERE NOT EXISTS (
        SELECT 1 FROM access_grant g
         WHERE g.principal_id = v_pid AND NOT g.is_deleted
           AND g.revoked_at IS NULL
           AND g.target_id IS NOT DISTINCT FROM w.target_id
           AND g.database_name IS NOT DISTINCT FROM w.database_name
           AND g.tier = w.tier AND g.auto_approve = w.auto_approve);

    -- A tier or window that moved: correct the row rather than churn it.
    UPDATE access_grant g SET valid_until = w.valid_until, db_role = w.db_role
      FROM (SELECT DISTINCT ON (target_id, database_name, tier, auto_approve) *
              FROM _want WHERE revoked_at IS NULL
             ORDER BY target_id, database_name, tier, auto_approve,
                      valid_until DESC NULLS FIRST) w
     WHERE g.principal_id = v_pid AND g.mirrored_from IS NOT NULL
       AND g.revoked_at IS NULL AND NOT g.is_deleted
       AND g.target_id IS NOT DISTINCT FROM w.target_id
       AND g.database_name IS NOT DISTINCT FROM w.database_name
       AND g.tier = w.tier AND g.auto_approve = w.auto_approve
       AND (g.valid_until IS DISTINCT FROM w.valid_until
            OR g.db_role IS DISTINCT FROM w.db_role);

    -- Roles.
    UPDATE role_assignment ra SET revoked_at = NOW()
     WHERE ra.principal_id = v_pid AND ra.mirrored_from = 'admins'
       AND ra.revoked_at IS NULL AND NOT ra.is_deleted
       AND NOT EXISTS (
           SELECT 1 FROM admins a
            WHERE a.slack_user_id = p_slack AND a.enabled
              AND (ra.role = 'admin' OR (ra.role = 'granter' AND a.can_grant)));

    INSERT INTO role_assignment
        (principal_id, role, all_teams, all_targets, max_tier, any_tier,
         reason, mirrored_from)
    SELECT v_pid, r.role, TRUE, TRUE,
           CASE WHEN r.role = 'admin' THEN a.max_tier END,
           CASE WHEN r.role = 'admin' THEN a.max_tier IS NULL ELSE TRUE END,
           'mirrored from admins', 'admins'
      FROM admins a
      CROSS JOIN LATERAL (VALUES ('admin'), ('granter')) AS r(role)
     WHERE a.slack_user_id = p_slack AND a.enabled
       AND (r.role = 'admin' OR a.can_grant)
       AND NOT EXISTS (
           SELECT 1 FROM role_assignment ra
            WHERE ra.principal_id = v_pid AND ra.role = r.role
              AND ra.revoked_at IS NULL AND NOT ra.is_deleted);

    UPDATE role_assignment ra
       SET max_tier = a.max_tier, any_tier = a.max_tier IS NULL
      FROM admins a
     WHERE a.slack_user_id = p_slack AND ra.principal_id = v_pid
       AND ra.role = 'admin' AND ra.mirrored_from = 'admins'
       AND ra.revoked_at IS NULL AND NOT ra.is_deleted
       AND (ra.max_tier IS DISTINCT FROM a.max_tier);

    -- Settings.
    DELETE FROM principal_setting s
     WHERE s.principal_id = v_pid AND s.mirrored_from IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM user_row_limit_overrides o
            WHERE o.slack_user_id = p_slack AND s.setting_key = 'max_rows')
       AND NOT EXISTS (
           SELECT 1 FROM report_excluded_users e
            WHERE e.slack_user_id = p_slack
              AND s.setting_key = 'exclude_from_metrics');

    INSERT INTO principal_setting
        (principal_id, setting_key, setting_value, valid_until, reason,
         mirrored_from)
    SELECT v_pid, 'max_rows', o.max_rows::text, o.expires_at,
           'mirrored from user_row_limit_overrides', 'user_row_limit_overrides'
      FROM user_row_limit_overrides o
     WHERE o.slack_user_id = p_slack
    ON CONFLICT DO NOTHING;

    UPDATE principal_setting s
       SET setting_value = o.max_rows::text, valid_until = o.expires_at
      FROM user_row_limit_overrides o
     WHERE o.slack_user_id = p_slack AND s.principal_id = v_pid
       AND s.setting_key = 'max_rows' AND NOT s.is_deleted
       AND (s.setting_value IS DISTINCT FROM o.max_rows::text
            OR s.valid_until IS DISTINCT FROM o.expires_at);

    INSERT INTO principal_setting
        (principal_id, setting_key, setting_value, reason, mirrored_from)
    SELECT v_pid, 'exclude_from_metrics', 'true',
           'mirrored from report_excluded_users', 'report_excluded_users'
      FROM report_excluded_users e
     WHERE e.slack_user_id = p_slack
    ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql;

-- Everything whose subject is one team.
CREATE OR REPLACE FUNCTION mirror_team(p_team_id INT) RETURNS void AS $$
DECLARE
    v_tid BIGINT;
BEGIN
    IF p_team_id IS NULL OR NOT mirror_enabled() THEN RETURN; END IF;
    PERFORM set_config('app.auth_mirror', 'on', true);

    SELECT t.id INTO v_tid FROM team t
     WHERE t.source = 'manual' AND t.external_id = p_team_id::text
       AND NOT t.is_deleted;
    IF v_tid IS NULL THEN
        INSERT INTO team (name, display_name, source, external_id)
        SELECT o.name, o.name, 'manual', o.id::text FROM teams o
         WHERE o.id = p_team_id
        RETURNING id INTO v_tid;
    END IF;
    IF v_tid IS NULL THEN RETURN; END IF;   -- the team was deleted outright

    UPDATE team t SET display_name = o.name, name = o.name
      FROM teams o WHERE o.id = p_team_id AND t.id = v_tid
       AND (t.name IS DISTINCT FROM o.name);

    -- Membership.
    DELETE FROM team_member tm
     WHERE tm.team_id = v_tid
       AND NOT EXISTS (
           SELECT 1 FROM team_members m
             JOIN principal_identity i ON i.external_id = m.slack_user_id
              AND i.provider = 'slack' AND NOT i.is_deleted
            WHERE m.team_id = p_team_id AND i.principal_id = tm.principal_id);

    INSERT INTO team_member (team_id, principal_id)
    SELECT v_tid, mirror_principal_id(m.slack_user_id)
      FROM team_members m WHERE m.team_id = p_team_id
    ON CONFLICT DO NOTHING;

    -- Team grants.
    UPDATE access_grant g SET revoked_at = NOW()
     WHERE g.team_id = v_tid AND g.mirrored_from = 'team_target_grants'
       AND g.revoked_at IS NULL AND NOT g.is_deleted
       AND NOT EXISTS (
           SELECT 1 FROM team_target_grants tg
             LEFT JOIN LATERAL unnest(COALESCE(tg.allowed_databases,
                                               ARRAY[NULL::text])) AS d(db) ON TRUE
            WHERE tg.team_id = p_team_id AND tg.revoked_at IS NULL
              AND tg.target_server_id IS NOT DISTINCT FROM g.target_id
              AND d.db IS NOT DISTINCT FROM g.database_name
              AND tg.mode = g.tier);

    INSERT INTO access_grant
        (team_id, target_id, all_targets, database_name, all_databases, tier,
         db_role, valid_from, valid_until, reason, mirrored_from)
    SELECT v_tid, tg.target_server_id, FALSE, d.db, d.db IS NULL, tg.mode,
           tg.target_role, tg.granted_at, tg.expires_at,
           'mirrored from team_target_grants', 'team_target_grants'
      FROM team_target_grants tg
      LEFT JOIN LATERAL unnest(COALESCE(tg.allowed_databases,
                                        ARRAY[NULL::text])) AS d(db) ON TRUE
     WHERE tg.team_id = p_team_id AND tg.revoked_at IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM access_grant g
            WHERE g.team_id = v_tid AND NOT g.is_deleted AND g.revoked_at IS NULL
              AND g.target_id IS NOT DISTINCT FROM tg.target_server_id
              AND g.database_name IS NOT DISTINCT FROM d.db
              AND g.tier = tg.mode AND NOT g.auto_approve);
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- the triggers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION mirror_by_user() RETURNS trigger AS $$
BEGIN
    PERFORM mirror_principal(COALESCE(NEW.slack_user_id, OLD.slack_user_id));
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION mirror_by_team() RETURNS trigger AS $$
BEGIN
    PERFORM mirror_team(COALESCE(NEW.team_id, OLD.team_id));
    -- A membership change moves access for that person too.
    IF TG_TABLE_NAME = 'team_members' THEN
        PERFORM mirror_principal(COALESCE(NEW.slack_user_id, OLD.slack_user_id));
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION mirror_by_teams_row() RETURNS trigger AS $$
BEGIN
    PERFORM mirror_team(COALESCE(NEW.id, OLD.id));
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['requesters', 'admins', 'user_target_grants',
                             'auto_approve_grants', 'user_row_limit_overrides',
                             'report_excluded_users']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_mirror ON %I', t);
        EXECUTE format(
            'CREATE TRIGGER trg_mirror AFTER INSERT OR UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION mirror_by_user()', t);
    END LOOP;

    FOREACH t IN ARRAY ARRAY['team_members', 'team_target_grants']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_mirror ON %I', t);
        EXECUTE format(
            'CREATE TRIGGER trg_mirror AFTER INSERT OR UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION mirror_by_team()', t);
    END LOOP;

    DROP TRIGGER IF EXISTS trg_mirror ON teams;
    CREATE TRIGGER trg_mirror AFTER INSERT OR UPDATE OR DELETE ON teams
        FOR EACH ROW EXECUTE FUNCTION mirror_by_teams_row();
END $$;
