-- Query fingerprint approval cache.
--
-- When a RO query a user already got approved + completed comes back
-- with the same parameter-agnostic fingerprint (see ast_safety.fingerprint:
-- literals normalized to placeholders), it auto-approves — no admin
-- round-trip for a repeat report whose only change is the parameter
-- values (WHERE id = 1 -> WHERE id = 2).
--
-- RO ONLY by design: for RW/DDL a literal change alters the real-world
-- effect while the fingerprint stays identical, so writes never
-- fingerprint-auto-approve. Scope: same requester + same target + same
-- database. TTL: a prior approval older than fingerprint_cache_ttl_days
-- no longer counts (schema / data drifts).

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS query_fingerprint TEXT;

COMMENT ON COLUMN requests.query_fingerprint IS
$$Parameter-agnostic fingerprint (ast_safety.fingerprint) of the query,
set at submit time for RO queries. Drives the fingerprint approval
cache: a repeat RO query with a prior completed match auto-approves.$$;

-- Lookup index for the cache hit check: find a completed RO request by
-- the same user, target, db, and fingerprint. Partial — only completed
-- rows with a fingerprint are ever probed.
CREATE INDEX IF NOT EXISTS idx_requests_fingerprint_cache
    ON requests (requester_slack_id, target_server_id, database_name,
                 query_fingerprint, completed_at)
 WHERE status = 'completed' AND query_fingerprint IS NOT NULL;

INSERT INTO bot_config (key, value, description) VALUES
    ('fingerprint_cache_enabled', 'on',
     'Auto-approve a repeat RO query whose parameter-agnostic fingerprint matches a prior completed request by the same user/target/db. RO only.'),
    ('fingerprint_cache_ttl_days', '30',
     'A prior completed approval older than this many days no longer counts toward fingerprint auto-approval.')
ON CONFLICT (key) DO NOTHING;
