-- Team membership from company-SSO groups (idp_group_sync.py).
--
-- An admin maps a team to one or more groups of an OIDC provider. At each
-- login through that provider, QueryHub adds the person to the mapped teams
-- whose group is in the `groups` claim, and removes the memberships that it
-- added before when the group is gone. A membership that a person made by
-- hand (source 'manual') is never touched. The switch
-- `idp_group_sync_enabled` is off by default.
--
-- `team_id` has no foreign key on purpose. The id names a row of `team`
-- while `access_model_v2` is on, and a row of `teams` while it is off. The
-- two tables use different id sequences. The code checks that the team
-- exists in the live model before it writes a mapping, and the sync joins
-- the live team table, so a mapping for a deleted team does nothing.

CREATE TABLE IF NOT EXISTS team_idp_groups (
    team_id     BIGINT      NOT NULL,
    provider    TEXT        NOT NULL CHECK (provider ~ '^[a-z0-9_]+$'),
    group_name  TEXT        NOT NULL CHECK (length(group_name) BETWEEN 1 AND 200),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  TEXT,
    PRIMARY KEY (team_id, provider, group_name)
);

CREATE INDEX IF NOT EXISTS idx_team_idp_groups_provider
    ON team_idp_groups (provider, group_name);

-- Where a membership came from. Existing rows get 'manual' from the default.
-- Both membership tables get the columns: `team_member` is the live model
-- while `access_model_v2` is on, `team_members` while it is off.
ALTER TABLE team_member  ADD COLUMN IF NOT EXISTS source    TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE team_member  ADD COLUMN IF NOT EXISTS idp_group TEXT;
ALTER TABLE team_members ADD COLUMN IF NOT EXISTS source    TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE team_members ADD COLUMN IF NOT EXISTS idp_group TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'team_member_source_check'
                      AND conrelid = 'team_member'::regclass) THEN
        ALTER TABLE team_member ADD CONSTRAINT team_member_source_check
            CHECK (source IN ('manual', 'idp'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'team_members_source_check'
                      AND conrelid = 'team_members'::regclass) THEN
        ALTER TABLE team_members ADD CONSTRAINT team_members_source_check
            CHECK (source IN ('manual', 'idp'));
    END IF;
END $$;

COMMENT ON COLUMN team_member.source IS
  'manual = an admin or an import added the row. idp = the company-SSO group '
  'sync added it at login (idp_group_sync.py). The sync removes only idp rows.';
COMMENT ON COLUMN team_member.idp_group IS
  'The SSO group that made the sync add this row. NULL for manual rows.';

INSERT INTO bot_config (key, value, description) VALUES
    ('idp_group_sync_enabled', 'off',
     'on = at each login through an OIDC provider, add the person to the teams mapped to their SSO groups and remove the memberships that the sync added before when the group is gone. Manual memberships are never touched. off = the sync does nothing.')
ON CONFLICT (key) DO NOTHING;
