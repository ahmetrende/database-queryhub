-- 077: indexes for the audit trail's real access patterns, and a sweep index
-- for expired web sessions.
--
-- WHY NOW: audit_log only had its primary key plus an index on request_id, but
-- nothing serves the two shapes the product actually issues:
--
--   1. the admin audit screen:      WHERE action = ANY($1) ORDER BY id DESC LIMIT n
--   2. the kill-switch banner:      WHERE action = 'kill_switch_set' ... ORDER BY id DESC LIMIT 1
--
-- Both currently scan. On this installation audit_log is still small, so the
-- plans are fine today — this is about the table that only ever grows, in every
-- deployment, and is read by a screen an operator refreshes. Adding the index
-- while it is cheap avoids the version where it isn't.
--
-- web_sessions is never swept: rows accumulate for every login forever. The
-- index makes the retention job's delete cheap; the job itself is in
-- scripts/cleanup_old_results.py.
--
-- Idempotent (CREATE INDEX IF NOT EXISTS) and additive: no data is rewritten,
-- and the indexes are small relative to the tables.

-- 1. Audit trail: action + recency. Covers `action = ANY(...) ORDER BY id DESC`
--    and the single-action lookups, which are the only filters the UI offers.
CREATE INDEX IF NOT EXISTS idx_audit_log_action_id
    ON audit_log (action, id DESC);

-- 2. Audit trail: recency alone, for the unfiltered "latest activity" reads.
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
    ON audit_log (created_at DESC);

-- 3. Expired / revoked sessions, for the retention sweep. Plain (not partial)
--    on purpose: a predicate cannot call NOW(), which is not IMMUTABLE, and
--    "expired" is inherently a moving comparison. Ordering by expires_at is
--    what the sweep needs anyway.
CREATE INDEX IF NOT EXISTS idx_web_sessions_expires_at
    ON web_sessions (expires_at);

-- 4. The auth-event outbox poller claims pending rows; make the claim ordered
--    and cheap even as the table grows in a deployment where the poller has
--    been down.
CREATE INDEX IF NOT EXISTS idx_auth_event_outbox_id_pending
    ON auth_event_outbox (id)
    WHERE processed_at IS NULL;
