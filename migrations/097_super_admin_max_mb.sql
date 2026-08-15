-- Seed `super_admin_max_mb`: the CSV size-cap floor for super-admins.
--
-- The sibling of `super_admin_max_rows` (migration 092), and it exists because
-- that one alone could not deliver what was asked for. `effective_caps` derives
-- the size cap FROM the row cap — `csv_size_mb * rows / max_rows`, trimmed by
-- `csv_size_mb_ceiling` — so size has never been expressible on its own. At
-- 100,000 rows the derivation yields 200 MB, and raising the ceiling could not
-- lift it past that: a ceiling only ever trims.
--
-- A FLOOR, like its sibling, applied as max(derived, this) AFTER the ceiling.
-- It outranks the ceiling deliberately: the ceiling is there to stop a
-- row-count override dragging the size cap up as a side effect, whereas this is
-- an operator naming the number on purpose.
--
-- 0 means inert, which is what every install gets until someone sets it, and it
-- can never lower anyone's cap.
--
-- Seeded rather than left to the code default because GET /admin/config builds
-- its groups from this table: an unseeded key is invisible and uneditable in
-- the admin UI, and tests/test_config_keys_seeded.py fails the build without it.

INSERT INTO bot_config (key, value, description) VALUES
('super_admin_max_mb', '0',
 'Result-file size cap FLOOR in MB for super-admins. The size cap is otherwise '
 'DERIVED from the row cap, so this is the only way to raise bytes without '
 'raising rows. Applied as max(derived size, this) after csv_size_mb_ceiling, '
 'which it outranks. 0 = inert; it can never lower a cap.')
ON CONFLICT (key) DO NOTHING;
