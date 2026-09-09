-- A tier ceiling that cannot be read must not be a ceiling that admits
-- everything.
--
-- `admins.max_tier` has carried CHECK (max_tier IN ('ro','rw','ddl')) since it
-- was added. `role_assignment`, the table that replaced it, was written with a
-- wildcard CHECK pairing (`any_tier = (max_tier IS NULL)`) and a role CHECK,
-- but never the vocabulary one -- so the new model lost a constraint the old
-- model had.
--
-- The reason it matters is the reader, not the writer. `access.can_approve`
-- compares ranks through `_TIER_RANK.get(value, 99)`, and an unrecognised
-- ceiling ranks 99: the comparison `request_rank > 99` is then false for every
-- request, so a ceiling nobody can parse admits ALL of them. A typo would not
-- narrow an approver's authority, it would remove the limit. The code is being
-- made fail-closed in the same change; this stops the row existing at all.
--
-- The web API already lower-cases and validates, so the exposed path is an
-- operator writing a role row in psql -- which is a documented, used path,
-- and the whole reason the mirror lives in triggers rather than in Python.
--
-- Measured before writing: the only live values are 'ro' and NULL, so this
-- validates without touching a row.

ALTER TABLE role_assignment
  DROP CONSTRAINT IF EXISTS role_assignment_max_tier_check;

ALTER TABLE role_assignment
  ADD CONSTRAINT role_assignment_max_tier_check
  CHECK (max_tier IS NULL OR max_tier IN ('ro', 'rw', 'ddl'));
