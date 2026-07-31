-- Two false positives that survived 088, both measured rather than guessed.
--
-- A follow-up file rather than an edit to 088: that migration is already
-- applied and the ledger records its checksum, so changing it in place would
-- make every future run report a modified migration.
--
-- After 088, a generic-region install flagged 2 innocent names out of 40:
--
--   pan              -> card     (a camera movement, a kitchen utensil)
--   tel_aviv_office  -> phone    ('tel' is the Turkish abbreviation)
--
-- 1. `pan` is the card industry's Primary Account Number, and as a BARE token
--    it is too ambiguous to be a default. Two facts make disabling it cheap:
--    it occurs ZERO times across the 99,479 column names catalogued from the
--    live fleet, and real card numbers are caught by the VALUE detector, which
--    Luhn-validates and checks the network prefix — so nothing that is actually
--    a card stops being masked. 088 additionally seeds `card_number` as an
--    unambiguous substring. An operator with a literal `pan` column can flip
--    this row back on; the row is kept (disabled) precisely so that is one
--    UPDATE rather than an INSERT they have to reconstruct.
--
-- 2. `tel` belongs with `telefon` in the tr region — 088 moved the long form
--    and missed the abbreviation. Generic keeps `phone`, `mobile`, `msisdn` and
--    the seeded `phone_number` / `mobile_number`, so no coverage is lost
--    outside Turkey, and inside Turkey the pattern is still active. Also zero
--    occurrences in the measured fleet.

UPDATE pii_column_patterns
   SET enabled = FALSE
 WHERE pattern = 'pan' AND match_type = 'token';

UPDATE pii_column_patterns
   SET region = 'tr'
 WHERE pattern = 'tel' AND match_type = 'token' AND region IS NULL;
