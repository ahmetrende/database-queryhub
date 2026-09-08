-- 114: `role_assignment` gains the `source` that `team` already has.
--
-- A role can now be written by a sync rather than by a person. The pod fleet
-- makes that necessary: a captain approves their own pod's requests to their
-- own pod's databases, `scope_target_id` holds ONE target, and a pod owns up
-- to seven — so five captains are twenty-three rows, and the day a pod gains
-- a service that set is wrong until somebody notices.
--
-- A sync cannot maintain rows it cannot recognise. `team.source` is exactly
-- this column for exactly this reason (scripts/import_teams.py owns the teams
-- it wrote and never touches 'manual'), and roles need the symmetry: own your
-- own rows, leave every other row alone.
--
-- NOT `mirrored_from`, which is already here and already means something else:
-- "the legacy table this row is a projection of". A projection of the admins
-- table and a row written by an org sync are different things with different
-- owners, and collapsing them would let the migration-109 mirror revoke a
-- synced role it never wrote.
--
-- NULL keeps meaning "a person wrote this by hand", which is every row that
-- exists today outside the mirror's.

ALTER TABLE role_assignment ADD COLUMN IF NOT EXISTS source TEXT;

COMMENT ON COLUMN role_assignment.source IS
  'Which sync owns this row, or NULL when a person wrote it directly. A sync '
  'may only update and revoke rows carrying its own source — the same rule '
  'team.source carries, and the same rule mirrored_from carries for the '
  'migration-109 mirror.';

CREATE INDEX IF NOT EXISTS idx_role_assignment_source
    ON role_assignment (source)
 WHERE source IS NOT NULL AND NOT is_deleted AND revoked_at IS NULL;
