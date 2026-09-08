-- Authorization-change notifications for the nine-table model.
--
-- Migration 060 put AFTER triggers on every table that could change what a
-- person may do, so that a change made in psql notifies its subject just as an
-- app path does. Those triggers are on the OLD tables. Without the same cover
-- on the new ones, the day the resolver switches over is the day authorization
-- starts changing silently — and silence is the exact failure 060 exists to
-- prevent. Attached now rather than at the switch, so the notifications are
-- already working when it happens.
--
-- The capture function needs one addition. It identifies its subject by
-- reading `slack_user_id` off the row, and the new tables do not have one:
-- they carry `principal_id`, a foreign key. Resolving that here rather than in
-- the poller keeps `auth_event_outbox.slack_user_id` populated exactly as
-- before, so the poller, the de-duplication and the DM path are untouched.
--
-- `team_id` needs no such translation, but it does need care from the reader:
-- `team_target_grants.team_id` points at `teams` and `access_grant.team_id`
-- points at `team`, which are different id spaces. The poller decides which to
-- look in from the table name, not from the number.

CREATE OR REPLACE FUNCTION auth_event_capture() RETURNS trigger AS $$
DECLARE
    v_old  JSONB;
    v_new  JSONB;
    v_user TEXT;
    v_team INT;
    v_pid  BIGINT;
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

    -- New model: the subject is a principal, and its Slack id lives one join
    -- away. A principal with no Slack identity — a machine account — resolves
    -- to NULL, which is correct: there is nobody to DM.
    IF v_user IS NULL THEN
        v_pid := COALESCE((v_new ->> 'principal_id')::bigint,
                          (v_old ->> 'principal_id')::bigint,
                          -- `principal` itself keys on `id`
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

-- The five new tables that can change what a person may do. `principal` is
-- included for `enabled`, which is the whitelist: switching it off removes
-- every access the person has, and that is worth a message.
--
-- `principal_identity` and `principal_credential` are deliberately NOT here.
-- They change how somebody proves who they are, not what they may reach, and
-- a credential row's contents should not travel into an outbox payload.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['access_grant', 'role_assignment', 'team_member',
                             'principal', 'principal_setting']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_auth_event ON %I', t);
        EXECUTE format(
            'CREATE TRIGGER trg_auth_event AFTER INSERT OR UPDATE OR DELETE '
            'ON %I FOR EACH ROW EXECUTE FUNCTION auth_event_capture()', t);
    END LOOP;
END $$;
