-- The person-name rule no longer masks a tier label.
--
-- A column named `tier_name` holds a label such as "standard". It does not
-- hold a person name. The `name` rule masked it, because the rule looks for the
-- token `name` and its exclusion list had no word for a tier. A query that
-- renamed the column, or wrapped it in an expression, kept the mask. The
-- catalog also follows the source column.
--
-- Add `tier` to the exclusions of the `name` rule. This only narrows the rule.
-- A real person-name column such as `first_name` or `full_name` still matches.
-- Measured on one real catalog: 43 column names contained `tier`. Three of them
-- stopped matching. All three have the name `tier_name`.
--
-- This is a new file, not an edit to 088. The ledger records the checksum of an
-- applied file. The guard in the WHERE clause makes a second run change
-- nothing.

UPDATE pii_column_patterns
   SET exclude_tokens = array_append(exclude_tokens, 'tier')
 WHERE pattern = 'name'
   AND pii_type = 'name'
   AND match_type = 'token'
   AND NOT ('tier' = ANY(exclude_tokens));
