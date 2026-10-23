-- 148: the migration-109 mirror learns the two new columns.
--
-- The mirror recomputes one person's rows in the new model from the old
-- tables. Two of its inputs gained a column, and a projection that ignores a
-- column is the bug class this codebase has met before (grant expiry,
-- migration 096): the old model stops a row, and its copy keeps deciding.
--
-- 1. auto_approve_grants.enabled (migration 146). A switched-off rule is not a
--    source. The copy of it in access_grant is revoked, so no reader of the new
--    model can find it live. Switching the rule on again writes a new copy.
--
-- 2. admins.expires_at (migration 147). It becomes role_assignment.valid_until
--    on both mirrored roles (admin, granter). A scope that has ended is treated
--    like a disabled one: its mirrored roles are revoked, and nothing is
--    written for it. A new copy is written only for a scope that is still
--    live, so valid_until is always later than valid_from (the
--    role_assignment_window_ordered CHECK).
--
-- Everything else in the function is unchanged from migration 109. The
-- function is recreated whole, because PostgreSQL has no way to replace part
-- of one.

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

    -- Migration 148: a switched-off rule is not a source.
    INSERT INTO _want
    SELECT a.target_server_id, a.target_server_id IS NULL, a.database_name,
           a.database_name IS NULL, a.max_tier, TRUE, TRUE, NULL,
           a.starts_at, a.expires_at, NULL, 'auto_approve_grants'
      FROM auto_approve_grants a
     WHERE a.slack_user_id = p_slack
       AND a.enabled
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

    -- Roles. Migration 148: a scope that has ended (admins.expires_at) is
    -- treated like a disabled one. Its mirrored roles are revoked, and no new
    -- one is written for it.
    UPDATE role_assignment ra SET revoked_at = NOW()
     WHERE ra.principal_id = v_pid AND ra.mirrored_from = 'admins'
       AND ra.revoked_at IS NULL AND NOT ra.is_deleted
       AND NOT EXISTS (
           SELECT 1 FROM admins a
            WHERE a.slack_user_id = p_slack AND a.enabled
              AND (a.expires_at IS NULL OR a.expires_at > NOW())
              AND (ra.role = 'admin' OR (ra.role = 'granter' AND a.can_grant)));

    -- The end date moved, or was set or cleared, on a role written before:
    -- correct it. Only for a live scope, so the new end is later than now and
    -- so later than the row's valid_from. Run before the INSERT below, so this
    -- touches existing rows only and the INSERT alone sets a new row's end.
    UPDATE role_assignment ra
       SET valid_until = a.expires_at
      FROM admins a
     WHERE a.slack_user_id = p_slack AND ra.principal_id = v_pid
       AND ra.mirrored_from = 'admins'
       AND ra.revoked_at IS NULL AND NOT ra.is_deleted
       AND a.enabled AND (a.expires_at IS NULL OR a.expires_at > NOW())
       AND ra.valid_until IS DISTINCT FROM a.expires_at;

    INSERT INTO role_assignment
        (principal_id, role, all_teams, all_targets, max_tier, any_tier,
         valid_until, reason, mirrored_from)
    SELECT v_pid, r.role, TRUE, TRUE,
           CASE WHEN r.role = 'admin' THEN a.max_tier END,
           CASE WHEN r.role = 'admin' THEN a.max_tier IS NULL ELSE TRUE END,
           a.expires_at,
           'mirrored from admins', 'admins'
      FROM admins a
      CROSS JOIN LATERAL (VALUES ('admin'), ('granter')) AS r(role)
     WHERE a.slack_user_id = p_slack AND a.enabled
       AND (a.expires_at IS NULL OR a.expires_at > NOW())
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

-- Bring every row the new columns could already affect into line now, rather
-- than at the next write for that person. Both columns are new and default to
-- "no change", so on a database where nobody has switched a rule off or set an
-- end this touches nothing. It is here for a database where someone did
-- between migrations 146/147 and this one.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT DISTINCT slack_user_id FROM auto_approve_grants WHERE NOT enabled
             UNION
             SELECT slack_user_id FROM admins WHERE expires_at IS NOT NULL
    LOOP
        PERFORM mirror_principal(r.slack_user_id);
    END LOOP;
END $$;
