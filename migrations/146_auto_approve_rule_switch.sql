-- 146: an auto-approve rule can be switched off, and on again.
--
-- Until now the only way to stop a rule was to delete it. A delete loses the
-- row, its reason and the count of the queries it approved, so a rule that had
-- to stop for one day was gone for good. `enabled` keeps the row and stops it
-- deciding.
--
-- A switched-off rule approves nothing. Every reader enforces it:
--   * the submit decision reads only switched-on rows (auto_approve.active_grants);
--   * v_active_auto_approve below lists only switched-on rows;
--   * the migration-109 mirror does not project a switched-off row into
--     access_grant (migration 148), so the new model holds no live copy of it.
--
-- A team's waiver has no row in auto_approve_grants: it lives only in
-- access_grant. So that table gets the same switch. Only a waiver can be
-- switched off. A grant is revoked, never paused, and the CHECK says so: a
-- reader of grant rows never has to ask about this column.
--
-- Every existing row stays on. This migration changes no decision.

ALTER TABLE auto_approve_grants
    ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN auto_approve_grants.enabled IS
    'FALSE = the rule is switched off: it approves nothing and the mirror '
    'keeps no live copy of it in access_grant. The row, its reason and its '
    'audit history stay. TRUE again = the rule decides again.';

ALTER TABLE access_grant
    ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;

DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conname = 'access_grant_only_a_waiver_switches_off'
         AND conrelid = 'access_grant'::regclass
  ) THEN
    ALTER TABLE access_grant
      ADD CONSTRAINT access_grant_only_a_waiver_switches_off
      CHECK (enabled OR auto_approve);
  END IF;
END $$;

COMMENT ON COLUMN access_grant.enabled IS
    'FALSE only on a waiver (auto_approve = TRUE): the waiver is switched off '
    'and skips no review. A grant row is always TRUE; a grant ends by revoke '
    'or by valid_until.';

-- Same columns, in the same order, as migration 051 wrote. Only the filter
-- changes, so CREATE OR REPLACE is enough.
CREATE OR REPLACE VIEW v_active_auto_approve AS
SELECT slack_user_id,
       max_tier,
       starts_at,
       expires_at,
       reason,
       granted_by,
       id AS grant_id,
       target_server_id,
       database_name
  FROM auto_approve_grants
 WHERE enabled
   AND starts_at <= NOW()
   AND (expires_at IS NULL OR expires_at > NOW());

COMMENT ON VIEW v_active_auto_approve IS
$$One row per active auto-approve grant: switched on, and NOW() inside [starts_at, expires_at). Multiple rows per user possible. Readers should pick the highest max_tier when summarising.$$;
