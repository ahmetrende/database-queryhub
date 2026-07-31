-- Region-gate the PII column catalog, and stop the bare `name` token from
-- flagging database metadata.
--
-- WHY. The seeded catalog mixes two kinds of pattern: unambiguous English ones
-- (full_name, email, iban) and Turkish ones (ad, adi, isim, soyad, eposta,
-- adres, telefon, cep, gsm, kimlik, vergi, dogum). The Turkish tokens are
-- ordinary words elsewhere -- `ad` is an advertisement, `cep` a region code,
-- `pan` a camera movement -- so a fresh install outside Turkey mangles data
-- that is not PII at all. Measured on a 27-name sample, 23 innocent columns
-- were flagged: product_name -> W***** M****, ad_id -> A******, and `cep`
-- came back as the empty string. It errs safe rather than leaking, but a tool
-- that corrupts a new user's first query is a tool they uninstall.
--
-- MEASURED before writing this, against 99,479 real column names catalogued
-- from the live fleet:
--
--   token 'ad'   0 occurrences      token 'pan'  0 occurrences
--   token 'tel'  0 occurrences      token 'cep'  0 occurrences
--   token 'name' 6,989 occurrences  (379 distinct names)
--
-- So region-gating the Turkish tokens costs this deployment NOTHING, and the
-- decision needed no guesswork. `name` is the opposite: heavily used, and
-- dominated by metadata rather than people --
--
--   database_name 1913   schema_name 773   table_name 673   host_name 216
--   application_name 216 program_name 206  index_name 158   object_name 101
--
-- against full_name 56, last_name 27, first_name 24, father_name 19. Dropping
-- the token outright would unmask the real ones (and bare `name`, 836
-- occurrences, which in an application table usually IS a person). So it keeps
-- matching, with an explicit exclusion list of metadata qualifiers taken from
-- the measured distribution.
--
-- Two new columns, both nullable so existing rows keep working:
--   region         NULL = every region; 'tr' = only when pii_region = 'tr'.
--   exclude_tokens a match is DISCARDED when the column name also contains one
--                  of these tokens. Narrowing, so it can only reduce false
--                  positives -- never mask less than the pattern would have on
--                  a name that has no qualifier.
--
-- This migration does NOT delete any pattern. On an existing install every row
-- survives; the Turkish ones simply carry their region, which on a pii_region
-- = 'tr' deployment leaves behavior identical. The narrower default is what a
-- FRESH install gets from the seed below.

ALTER TABLE pii_column_patterns
    ADD COLUMN IF NOT EXISTS region TEXT;

ALTER TABLE pii_column_patterns
    ADD COLUMN IF NOT EXISTS exclude_tokens TEXT[];

COMMENT ON COLUMN pii_column_patterns.region IS
    'NULL = applies in every region. Otherwise the bot_config pii_region value '
    'this pattern belongs to (e.g. ''tr''), so language-specific tokens do not '
    'fire on installs where they are ordinary words.';

COMMENT ON COLUMN pii_column_patterns.exclude_tokens IS
    'If the column name contains any of these tokens, the match is discarded. '
    'Used to keep the broad ''name'' token off database metadata '
    '(database_name, schema_name, table_name...). Narrowing only.';

-- Turkish-language tokens: ordinary words in other languages.
UPDATE pii_column_patterns SET region = 'tr'
 WHERE region IS NULL
   AND pattern IN ('ad', 'adi', 'isim', 'ismi', 'soyad', 'soyadi',
                   'eposta', 'adres', 'telefon', 'cep', 'gsm',
                   'kimlik', 'vergi', 'dogum', 'tckn', 'vkn');

-- The broad English tokens, narrowed by the metadata qualifiers measured
-- above. `name` also excludes 'db' (db_name) and 'column'/'constraint'/'slot'
-- and friends; the list is deliberately about DATABASE metadata, which is what
-- a SQL gateway sees most of.
UPDATE pii_column_patterns
   SET exclude_tokens = ARRAY[
        'database', 'db', 'schema', 'table', 'column', 'index', 'object',
        'constraint', 'sequence', 'partition', 'slot', 'subscription',
        'publication', 'tablespace', 'role', 'trigger', 'view', 'function',
        'procedure', 'extension', 'type', 'domain', 'operator', 'collation',
        'host', 'server', 'application', 'program', 'service', 'process',
        'file', 'path', 'directory', 'bucket', 'queue', 'topic', 'job',
        'task', 'step', 'stage', 'parameter', 'param', 'setting', 'config',
        'option', 'flag', 'variable', 'env', 'key', 'field',
        'product', 'category', 'brand', 'country', 'city', 'currency',
        'status', 'state', 'event', 'action', 'type', 'kind', 'class',
        'group', 'channel', 'campaign', 'template', 'report', 'metric',
        'tag', 'label', 'display', 'method', 'provider', 'vendor',
        'instrument', 'blocker', 'session', 'device', 'browser', 'os']
 WHERE pattern = 'name' AND match_type = 'token'
   AND exclude_tokens IS NULL;

-- `pan` is the card industry's Primary Account Number, but as a bare token it
-- is also a camera movement and a kitchen utensil, and it matched nothing at
-- all in the measured fleet. Actual card numbers are still caught by the VALUE
-- detector, which Luhn-validates, so narrowing this costs no real coverage.
UPDATE pii_column_patterns
   SET exclude_tokens = ARRAY['angle', 'tilt', 'zoom', 'speed', 'position',
                              'axis', 'direction', 'camera', 'view']
 WHERE pattern = 'pan' AND match_type = 'token'
   AND exclude_tokens IS NULL;

-- `mobile` is a phone in one context and a platform in another.
UPDATE pii_column_patterns
   SET exclude_tokens = ARRAY['app', 'platform', 'device', 'os', 'version',
                              'sdk', 'client', 'browser', 'flag', 'enabled']
 WHERE pattern = 'mobile' AND match_type = 'token'
   AND exclude_tokens IS NULL;

-- `birth` as a substring hits birth_certificate_template and similar.
UPDATE pii_column_patterns
   SET exclude_tokens = ARRAY['certificate', 'template', 'country', 'place',
                              'city', 'hospital', 'rate', 'order']
 WHERE pattern = 'birth'
   AND exclude_tokens IS NULL;

-- `addr` / `address` also name network addresses.
UPDATE pii_column_patterns
   SET exclude_tokens = ARRAY['ip', 'mac', 'host', 'server', 'client',
                              'peer', 'socket', 'endpoint', 'contract',
                              'wallet', 'network', 'gateway', 'bind',
                              'listen', 'remote', 'local', 'check']
 WHERE pattern IN ('addr', 'address')
   AND exclude_tokens IS NULL;

-- Seed for a FRESH install: unambiguous multi-token names that need no
-- exclusion list at all. ON CONFLICT DO NOTHING, so an existing catalog is
-- untouched.
INSERT INTO pii_column_patterns (pattern, pii_type, match_type, enabled, region)
VALUES
    ('full_name',     'name',      'substring', TRUE, NULL),
    ('first_name',    'name',      'substring', TRUE, NULL),
    ('last_name',     'name',      'substring', TRUE, NULL),
    ('middle_name',   'name',      'substring', TRUE, NULL),
    ('given_name',    'name',      'substring', TRUE, NULL),
    ('family_name',   'name',      'substring', TRUE, NULL),
    ('legal_name',    'name',      'substring', TRUE, NULL),
    ('email_address', 'email',     'substring', TRUE, NULL),
    ('home_address',  'address',   'substring', TRUE, NULL),
    ('postal_address', 'address',  'substring', TRUE, NULL),
    ('street_address', 'address',  'substring', TRUE, NULL),
    ('billing_address', 'address', 'substring', TRUE, NULL),
    ('shipping_address', 'address', 'substring', TRUE, NULL),
    ('date_of_birth', 'birthdate', 'substring', TRUE, NULL),
    ('birth_date',    'birthdate', 'substring', TRUE, NULL),
    ('birthdate',     'birthdate', 'substring', TRUE, NULL),
    ('phone_number',  'phone',     'substring', TRUE, NULL),
    ('mobile_number', 'phone',     'substring', TRUE, NULL),
    ('card_number',   'card',      'substring', TRUE, NULL),
    ('account_number', 'iban',     'substring', TRUE, NULL)
ON CONFLICT DO NOTHING;
