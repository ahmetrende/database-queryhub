-- Add missing phone column-name patterns.
--
-- The initial seed (043) had phone / telefon / gsm / cep / msisdn but
-- not the very common English 'mobile' (nor 'tel'). A column literally
-- named `mobile` holding a bare 10-digit number slipped through both
-- layers: content detection is conservative about bare digit runs, and
-- the column catalog had no matching pattern.

INSERT INTO pii_column_patterns (pattern, pii_type, match_type, notes) VALUES
    ('mobile', 'phone', 'token', 'mobile number (EN)'),
    ('tel',    'phone', 'token', 'telephone'),
    ('phone',  'phone', 'token', 'phone number')
ON CONFLICT (pattern, match_type) DO NOTHING;
