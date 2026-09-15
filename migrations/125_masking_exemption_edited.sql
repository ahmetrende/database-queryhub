-- An exemption can now be edited in place, so the row has to say by whom and
-- when. Without it the screen can only ever show who CREATED a row, and the
-- one field most likely to be edited is the reason -- the field whose whole
-- job is to tell the next reader who decided this and why.
--
-- Nullable on purpose: every existing row was never edited, and a default of
-- created_at/created_by would state an edit that did not happen.
ALTER TABLE pii_masking_exemptions
    ADD COLUMN IF NOT EXISTS updated_by text,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz;

COMMENT ON COLUMN pii_masking_exemptions.updated_by IS
    'Principal who last edited this row in place; NULL means never edited.';
COMMENT ON COLUMN pii_masking_exemptions.updated_at IS
    'When this row was last edited in place; NULL means never edited.';
