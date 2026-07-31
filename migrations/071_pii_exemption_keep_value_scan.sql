-- Soft PII exemption: lift the column-NAME mask but keep the value scan.
--
-- A full pii_masking_exemptions row makes a column pass through untouched —
-- no column-name masking AND no content scan. That is wrong for columns that
-- are MOSTLY non-sensitive but can hold the odd real PII: e.g. a crypto
-- deposit-address column that also stores fiat IBANs for bank rows. Blanket
-- exempting it would expose those IBANs.
--
-- keep_value_scan = true marks a COLUMN-level exemption as "soft": drop the
-- column-name rule (so genuine non-PII values like crypto addresses pass
-- through) but STILL run the per-value detectors, so any IBAN / card / TCKN /
-- email in an individual cell is masked. Only meaningful on column-level rows
-- (table_name/column_name set); ignored for table/db-wide lift rows.
ALTER TABLE pii_masking_exemptions
  ADD COLUMN IF NOT EXISTS keep_value_scan BOOLEAN NOT NULL DEFAULT FALSE;
