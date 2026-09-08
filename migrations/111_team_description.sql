-- 111: the new `team` table gains the description the old one always had.
--
-- Found while routing the Slack team views through the model switch. `teams`
-- has a `description`, `team` does not, and the copy had nowhere to put it:
-- all six rows carry `attributes = {}` and a `display_name` set to the name.
-- So on the day `access_model_v2` flips, every team description disappears
-- from every surface that reads the new model, with nothing failing.
--
-- Not academic. One of the six is operational documentation of what the team
-- is FOR: its description names the databases its grant covers and the tier.
-- That is the sentence an admin reads before deciding whether somebody belongs
-- in it.
--
-- A first-class column rather than a key in `attributes`: every team has one,
-- three surfaces render it (the Slack team view, the admin Teams screen, and
-- the team form writes it), and burying a displayed field in JSONB makes every
-- reader spell `attributes->>'description'` and every writer remember to merge
-- rather than replace.

ALTER TABLE team ADD COLUMN IF NOT EXISTS description TEXT;

COMMENT ON COLUMN team.description IS
  'What the team is for, in a sentence. Carried from teams.description by the '
  'migration-109 mirror for source=manual rows; set directly for any other '
  'source.';

-- Backfill what the copy could not carry.
UPDATE team t SET description = o.description
  FROM teams o
 WHERE t.source = 'manual' AND t.external_id = o.id::text
   AND NOT t.is_deleted AND t.description IS NULL
   AND o.description IS NOT NULL;

-- ---------------------------------------------------------------------------
-- the mirror has to carry it too, or the next edit drops it again
-- ---------------------------------------------------------------------------
--
-- Two changes below, and the second is the one that would have been missed:
-- the UPDATE was guarded on the NAME alone, so editing only a team's
-- description left the projection untouched and the two models disagreeing
-- until something else about that team changed.

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
        INSERT INTO team (name, display_name, description, source, external_id)
        SELECT o.name, o.name, o.description, 'manual', o.id::text FROM teams o
         WHERE o.id = p_team_id
        RETURNING id INTO v_tid;
    END IF;
    IF v_tid IS NULL THEN RETURN; END IF;   -- the team was deleted outright

    UPDATE team t SET display_name = o.name, name = o.name,
                      description = o.description, updated_at = now()
      FROM teams o WHERE o.id = p_team_id AND t.id = v_tid
       AND (t.name IS DISTINCT FROM o.name
            OR t.description IS DISTINCT FROM o.description);

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
