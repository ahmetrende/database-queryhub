-- Trigram indexes for the audit trail's free-text search.
--
-- The search is `ILIKE '%term%'` across seven columns, and a leading wildcard
-- has no prefix to seek, so no btree can serve it. pg_trgm indexes the text as
-- overlapping three-character pieces, which a GIN index CAN answer.
--
-- THE INDEX ALONE DOES NOTHING, and that is worth recording because it was
-- measured the hard way. Written as one OR spanning audit_log, requests and
-- target_servers, the predicate is applied AFTER the join, so the planner
-- ignored all four of these indexes and sequentially scanned 26k rows on every
-- keystroke — 420 ms, unchanged by their existence. The search had to be
-- rewritten as a UNION of single-table branches (see `_AUDIT_SEARCH_SQL`)
-- before any of them was used. With both halves: 420 ms -> 103 ms on the raw
-- query, 490 ms -> 255 ms end to end, with results verified identical across
-- ten terms including the match-everything and match-nothing cases.
--
-- Applied to production on 2026-09-09 with CREATE INDEX CONCURRENTLY, so no
-- write to audit_log was blocked. They are written plainly here because a
-- fresh install has no rows to lock and CONCURRENTLY cannot run inside the
-- migration runner's transaction. `IF NOT EXISTS` makes this a no-op on the
-- box where they already exist.
--
-- Cost: about 9 MB today, and GIN maintenance on every audit_log insert —
-- softened by the default fastupdate pending list. Worth it because the table
-- grows by roughly a thousand rows a day, and the seq scan grows with it.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS ix_audit_log_action_trgm
    ON audit_log USING gin (action gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ix_audit_log_actor_trgm
    ON audit_log USING gin (actor_name gin_trgm_ops);

-- The payload is where most searches actually land: it holds the ids, the
-- before/after values and the sentences explaining what was measured.
CREATE INDEX IF NOT EXISTS ix_audit_log_details_trgm
    ON audit_log USING gin ((details::text) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ix_requests_query_trgm
    ON requests USING gin (query gin_trgm_ops);
