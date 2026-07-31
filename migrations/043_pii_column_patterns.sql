-- Column-name PII catalog.
--
-- Content-based detection (pii.py DETECTORS) catches values with a
-- recognizable format (email, phone, TCKN, VKN, IBAN, card). But
-- free-text PII — names, addresses — has no format, so it can only be
-- caught by the COLUMN it lives in. This table is that catalog: a
-- result column whose name matches a pattern here gets its values
-- masked per the pattern's pii_type.
--
-- Matching (default 'token'): the column name is lowercased and split
-- on _ - and whitespace; if any token exactly equals the pattern, it
-- matches. So 'customer_email' and 'full_name' match 'email' / 'name'.
-- match_type can later be 'substring' or 'regex' for special cases.
--
-- Operators add a row here when a new / differently-named PII column
-- shows up — no code change, runtime-effective.

CREATE TABLE IF NOT EXISTS pii_column_patterns (
    id          BIGSERIAL PRIMARY KEY,
    pattern     TEXT NOT NULL,                 -- lowercase token to match
    pii_type    TEXT NOT NULL,                 -- email|phone|tckn|vkn|iban|card|name|address|generic
    match_type  TEXT NOT NULL DEFAULT 'token', -- token|substring|regex
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pattern, match_type)
);

COMMENT ON TABLE pii_column_patterns IS
$$Column-name PII catalog. A result column whose name matches a pattern
here is masked by pii_type. Covers free-text PII (name/address) that
content-based detection can't see; operators extend it without a code
change.$$;

-- Seed common TR + EN column-name patterns. Format-bearing types
-- (email, phone, tckn, vkn, iban, card) match by exact token — a
-- belt-and-suspenders layer on top of content detection. Free-text
-- types (name, address) match by SUBSTRING so Turkish inflectional
-- suffixes are caught: 'ev_adresi' / 'fatura_adresi' all contain
-- 'adres'. Name keeps token matching (substring 'name' would catch
-- file_name etc.) but seeds the common inflected forms.
INSERT INTO pii_column_patterns (pattern, pii_type, match_type, notes) VALUES
    ('email',    'email',   'token',     'email address'),
    ('mail',     'email',   'token',     'email address'),
    ('eposta',   'email',   'token',     'email address (TR)'),
    ('phone',    'phone',   'token',     'phone number'),
    ('telefon',  'phone',   'token',     'phone number (TR)'),
    ('gsm',      'phone',   'token',     'mobile number (TR)'),
    ('cep',      'phone',   'token',     'mobile number (TR)'),
    ('msisdn',   'phone',   'token',     'mobile number'),
    ('tckn',     'tckn',    'token',     'Turkish national ID'),
    ('kimlik',   'tckn',    'token',     'Turkish national ID (TR)'),
    ('vkn',      'vkn',     'token',     'Turkish tax number'),
    ('vergi',    'vkn',     'token',     'Turkish tax number (TR)'),
    ('iban',     'iban',    'token',     'bank account IBAN'),
    ('pan',      'card',    'token',     'card primary account number'),
    ('name',     'name',    'token',     'person name'),
    ('ad',       'name',    'token',     'person name (TR)'),
    ('adi',      'name',    'token',     'person name, inflected (TR)'),
    ('isim',     'name',    'token',     'person name (TR)'),
    ('ismi',     'name',    'token',     'person name, inflected (TR)'),
    ('soyad',    'name',    'token',     'surname (TR)'),
    ('soyadi',   'name',    'token',     'surname, inflected (TR)'),
    ('surname',  'name',    'token',     'surname'),
    ('fullname', 'name',    'token',     'full name'),
    ('adres',    'address', 'substring', 'postal address (TR) — matches ev_adresi etc.'),
    ('address',  'address', 'substring', 'postal address'),
    ('addr',     'address', 'substring', 'postal address')
ON CONFLICT (pattern, match_type) DO NOTHING;
