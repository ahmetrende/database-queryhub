-- SEC-ENG: persist the engine and the engine-aware required tier on each
-- request.
--
-- The tier (ro/rw/dll) is derived from the query's leading keyword, and that
-- classification is engine-specific: T-SQL MERGE, DENY, and other non-Postgres
-- forms are misread by the Postgres parser. Submit time already classifies
-- with the target's engine, but the approval-time scope check re-derived the
-- tier from the raw query text with the Postgres parser, so a non-Postgres
-- query could be admitted under the wrong tier.
--
-- Snapshot both on the row so approval and audit read the engine-correct
-- value instead of re-deriving it blind:
--   engine         - the target engine at submit time
--   required_tier  - the engine-aware tier computed at submit
--   executed_tier  - the tier the executor actually connected with

ALTER TABLE requests ADD COLUMN IF NOT EXISTS engine        text;
ALTER TABLE requests ADD COLUMN IF NOT EXISTS required_tier text;
ALTER TABLE requests ADD COLUMN IF NOT EXISTS executed_tier text;
