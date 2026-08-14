-- Seed `super_admin_max_rows`: the row-cap FLOOR for super-admins.
--
-- A super-admin runs without tier gates and without an approver, but keeps a row
-- cap. That cap is a RESOURCE guard, not an authorization one — it is what stops
-- a mistyped SELECT from filling the disk — so it is raised by configuration
-- rather than removed.
--
-- A FLOOR, never a ceiling: `row_limits.effective_caps` takes
-- max(max_rows, this, per-user override), so a value at or below `max_rows`
-- changes nothing. 1000 is the same default the code passes, which makes this
-- row inert on every install until an operator raises it deliberately — and it
-- can never lower anyone's cap.
--
-- Why seed at all, when the code already defaults: GET /admin/config builds its
-- groups from this table, so an unseeded key is invisible and uneditable in the
-- admin UI. tests/test_config_keys_seeded.py fails the build without this.

INSERT INTO bot_config (key, value, description) VALUES
('super_admin_max_rows', '1000',
 'Result-row cap FLOOR for super-admins, who run without tier gates and '
 'without approval but not without a cap. Applied as '
 'max(max_rows, this, per-user override), so a value at or below max_rows is '
 'inert and this can never lower a cap. Raise it when the DBA needs larger '
 'result sets than the fleet default.')
ON CONFLICT (key) DO NOTHING;
