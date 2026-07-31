-- PII masking toggle.
--
-- The bot masks sensitive values (email, Turkish mobile phone) in the
-- result file as it streams, using content-based detection (regex +
-- validators) in src/queryhub/pii.py. Detection is by value, not
-- column name, so aliased / wrapped columns can't slip PII past the
-- mask. Which detector fired is recorded in audit_log.details
-- ("pii_masked": [...]) and surfaced to the requester in the result DM.
--
-- Runtime-effective: flip to 'off' to disable masking without a restart.

INSERT INTO bot_config (key, value, description) VALUES
    ('pii_masking_enabled', 'on',
     'Mask PII (email / phone) in query result files. Content-based; on by default. Set to off to disable.')
ON CONFLICT (key) DO NOTHING;
