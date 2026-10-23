-- 149: a masking exemption can end on a date, and can be for a team or for
-- the DBAs.
--
-- Two new dimensions, read by pii._load_exemptions on every result:
--
--   * expires_at -- after this moment the row is not applied, so masking comes
--     back. NULL = no end (the DBA toolkit row from migration 090 keeps NULL).
--     Evaluated in SQL at read time, never by a sweep, for the same reason as
--     grant expiry (migration 096): a job that has not run yet is a window in
--     which the answer is wrong.
--
--   * audience -- who the row unmasks for:
--       everyone  every reader (what super_admin_only = FALSE always meant);
--       super     super-admins only (what super_admin_only = TRUE meant);
--       dba       holders of an admin role (access.is_admin under
--                 access_model_v2), super-admins included;
--       team      members of audience_team_id. Needs access_model_v2: the
--                 id is a `team` row, the one the Teams screen lists.
--     A reader outside the audience keeps full masking.
--
-- super_admin_only stays, and every writer keeps it equal to
-- (audience = 'super'). The reader is fail-closed about the pair: a row where
-- EITHER says super applies only to super-admins. So an operator who still
-- sets the old flag from psql narrows the row, and never widens it.
--
-- Every existing row keeps its meaning: rows with the flag become 'super',
-- every other row 'everyone', and no row gets an end.

ALTER TABLE pii_masking_exemptions
    ADD COLUMN IF NOT EXISTS expires_at       timestamptz,
    ADD COLUMN IF NOT EXISTS audience         text NOT NULL DEFAULT 'everyone',
    ADD COLUMN IF NOT EXISTS audience_team_id bigint REFERENCES team (id);

DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conname = 'pii_masking_exemptions_audience_check'
         AND conrelid = 'pii_masking_exemptions'::regclass
  ) THEN
    ALTER TABLE pii_masking_exemptions
      ADD CONSTRAINT pii_masking_exemptions_audience_check
      CHECK (audience IN ('everyone', 'super', 'dba', 'team'));
  END IF;
  IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conname = 'pii_masking_exemptions_team_audience_names_a_team'
         AND conrelid = 'pii_masking_exemptions'::regclass
  ) THEN
    ALTER TABLE pii_masking_exemptions
      ADD CONSTRAINT pii_masking_exemptions_team_audience_names_a_team
      CHECK ((audience = 'team') = (audience_team_id IS NOT NULL));
  END IF;
END $$;

COMMENT ON COLUMN pii_masking_exemptions.expires_at IS
    'When this exemption stops applying, so masking comes back. NULL = never. '
    'Evaluated by the reader (pii._load_exemptions) at read time.';
COMMENT ON COLUMN pii_masking_exemptions.audience IS
    'Who the exemption unmasks for: everyone, super (super-admins), dba '
    '(admin role holders, super-admins included) or team (members of '
    'audience_team_id). Everyone else keeps full masking. A row where this or '
    'super_admin_only says super applies to super-admins only.';
COMMENT ON COLUMN pii_masking_exemptions.audience_team_id IS
    'The team a team-audience row is for (a `team` row). NULL for every other '
    'audience.';

-- The old flag, carried into the new column. Silent: this changes no row's
-- meaning, so it must not DM every admin that an exemption changed.
SELECT set_config('app.auth_dm_suppress', 'on', true);
UPDATE pii_masking_exemptions
   SET audience = 'super'
 WHERE super_admin_only AND audience = 'everyone';
SELECT set_config('app.auth_dm_suppress', '', true);
