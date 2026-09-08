-- Two corrections to 105, both found by measuring the rows the copy has to
-- carry rather than by reasoning about the design.
--
-- ---------------------------------------------------------------------------
-- 1. An admin may have a tier ceiling
-- ---------------------------------------------------------------------------
--
-- 105 said `role = 'admin'` implies `any_tier`, on the reasoning that a
-- half-super-admin is hard to reason about. Production disagrees: of the two
-- admin rows, one has `max_tier = 'ro'`. That person has full DDL *access* and
-- sees every target including disabled ones — `is_admin()` short-circuits the
-- resolver — but may only *approve* read requests. Under 105's constraint they
-- were inexpressible, and the copy would have had to either widen what they can
-- approve or narrow what they can reach.
--
-- So the two halves separate. An admin's SCOPE stays fleet-wide, because that
-- is what distinguishes the role from `approver`: an administrator who is
-- responsible for some teams and not others is an approver, and says so. But
-- `max_tier` caps what they may approve, exactly as the `admins` table does
-- today, and does not touch their access.
ALTER TABLE role_assignment
  DROP CONSTRAINT IF EXISTS role_assignment_admin_is_unscoped;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'role_assignment_admin_is_fleet_wide'
       AND conrelid = 'role_assignment'::regclass
  ) THEN
    ALTER TABLE role_assignment
      ADD CONSTRAINT role_assignment_admin_is_fleet_wide
      CHECK (role <> 'admin' OR (all_teams AND all_targets));
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. An auto-approve row waives the wait; it does not grant the access
-- ---------------------------------------------------------------------------
--
-- No schema change — a rule the resolver must follow, recorded here because
-- getting it wrong hands out fleet-wide access and the reason is not visible
-- from the columns.
--
-- Today `auto_approve_grants` is consulted only AFTER the access check has
-- already passed, so a window on a target the person cannot reach does nothing
-- at all. Folding those rows into `access_grant` makes them look like grants,
-- and if the resolver counted them as such the copy alone would widen access
-- for four of the thirty-two live windows: three are fleet-wide `ro` windows
-- held by people who have no fleet-wide access, and one names a target its
-- holder cannot reach.
--
-- Therefore, in `access.resolve`:
--
--   allowed tier = the highest tier among covering rows with auto_approve FALSE
--   auto tier    = the highest tier among covering rows with auto_approve TRUE,
--                  capped at the allowed tier
--
-- A row is a grant or a waiver, never both; "RW, and do not make me wait" is
-- two rows. That is also what the two source tables say, so the copy is a
-- transcription rather than an interpretation.
COMMENT ON COLUMN access_grant.auto_approve IS
  'TRUE = this row waives the approval wait up to its tier. It does NOT grant '
  'access: the allowed tier comes only from rows where this is FALSE, and the '
  'auto tier is capped at it. A window on a target the principal cannot reach '
  'does nothing, which is how auto_approve_grants behaves today.';

COMMENT ON COLUMN role_assignment.max_tier IS
  'Ceiling on what this role may APPROVE. It does not limit access: an admin '
  'with max_tier ro still reaches every target at ddl, exactly as an admins '
  'row with max_tier does today.';

COMMENT ON COLUMN role_assignment.role IS
  'admin = fleet-wide: access to every target including disabled ones, sees '
  'the whole catalog, approves up to max_tier. approver = approves only, '
  'within scope, grants no access. granter = may write grants. importer = may '
  'import CSV. Only admin carries access.';
