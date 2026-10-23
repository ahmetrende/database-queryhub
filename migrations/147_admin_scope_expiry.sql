-- 147: an admin scope can end on a date.
--
-- `admins` rows had no end. A DBA who covers one database for a week kept the
-- scope until somebody remembered to remove it. `expires_at` ends it: after
-- that moment the row decides nothing, as if it were disabled.
--
-- The end is enforced where the scope is read:
--   * under access_model_v2, by `role_assignment.valid_until`. The mirror
--     projects this column there (migration 148), and access.roles,
--     can_approve, list_admins and every role reader already filter on it;
--   * under the legacy model, by admins.py, grants.py and the web admin
--     screens, which now filter on this column.
--
-- A super-admin never ends. The new model already refuses one with an end
-- (access.is_super_admin needs valid_until NULL), and the old model never let
-- a temporary admin be a super-admin. The CHECK keeps the two models in step:
-- a row with every scope column NULL (the legacy definition of a super-admin)
-- cannot carry an end.
--
-- Every existing row gets NULL: no end. This migration changes no decision.

ALTER TABLE admins
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

COMMENT ON COLUMN admins.expires_at IS
    'When this admin scope stops applying. NULL = never. After it the row '
    'decides nothing, as if enabled were FALSE. Projected into '
    'role_assignment.valid_until by the migration-109 mirror (migration 148).';

DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conname = 'admins_super_admin_never_ends'
         AND conrelid = 'admins'::regclass
  ) THEN
    ALTER TABLE admins
      ADD CONSTRAINT admins_super_admin_never_ends
      CHECK (expires_at IS NULL
             OR max_tier IS NOT NULL
             OR scope_team_ids IS NOT NULL
             OR scope_target_ids IS NOT NULL);
  END IF;
END $$;

-- The auth-event trigger (migration 060) fires on an UPDATE of a named list of
-- columns. A new end, written from psql with nothing else, must tell the
-- person too, so the column joins the list. Same statement as 060 otherwise.
DROP TRIGGER IF EXISTS trg_auth_event ON admins;
CREATE TRIGGER trg_auth_event
    AFTER INSERT OR UPDATE OF enabled, can_grant, max_tier, scope_team_ids,
                              scope_target_ids, expires_at
       OR DELETE ON admins
    FOR EACH ROW EXECUTE FUNCTION auth_event_capture();
