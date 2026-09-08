-- 110: one live role row per (person, role, scope), and no ceiling that
-- nothing enforces.
--
-- Two gaps the Roles screen made visible. Both are about a row that can be
-- written, is read back, and does not mean what it looks like.
--
-- 1. There was no uniqueness on role_assignment, so a second POST with the
--    same subject and scope wrote a second live row. `access_grant` has had
--    `access_grant_live_uq` since migration 105 for exactly this reason; roles
--    were the half that never got it. A role is a statement of authority and
--    the model treats one as immutable — changing it means revoking and
--    creating, which only holds if the same statement cannot be made twice.
--    Two rows differing only in `max_tier` are the worse case: `can_approve`
--    admits on ANY covering row, so the wider of the pair silently wins and
--    the narrower one reads, on screen, like a limit that is in force.
--
-- 2. `max_tier` is read by `can_approve`, which considers 'admin' and
--    'approver' rows only. Stored on a 'granter' or an 'importer' it is
--    inert — a ceiling that appears in the API, renders on the screen, and
--    limits nothing. Refusing it is better than carrying it: the day granting
--    grows a ceiling, this constraint is what makes someone decide, rather
--    than a column that was quietly right all along.
--
-- Measured against production before applying: 3 live role rows, 0 duplicate
-- scopes, 0 ceilings outside admin/approver. Nothing to clean up first.

CREATE UNIQUE INDEX IF NOT EXISTS role_assignment_live_uq
  ON role_assignment (principal_id, role, scope_team_id, scope_target_id)
  NULLS NOT DISTINCT
  WHERE NOT is_deleted AND revoked_at IS NULL;

COMMENT ON INDEX role_assignment_live_uq IS
  'One live role row per person, role and scope. Revoked and soft-deleted '
  'rows are outside it, so the history of who could approve what is kept.';

DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conname = 'role_assignment_ceiling_is_enforceable'
         AND conrelid = 'role_assignment'::regclass
  ) THEN
    ALTER TABLE role_assignment
      ADD CONSTRAINT role_assignment_ceiling_is_enforceable
      CHECK (max_tier IS NULL OR role IN ('admin', 'approver'));
  END IF;
END $$;

COMMENT ON COLUMN role_assignment.max_tier IS
  'The highest tier this role may act at, or NULL for no ceiling. Only '
  'read for admin and approver rows (access.can_approve), and the '
  'role_assignment_ceiling_is_enforceable constraint keeps it off the '
  'roles where it would be inert.';
