-- Add 'scheduled' to the request_status enum so requests with a future
-- `scheduled_for` can sit in queue between approval and execution time.
--
-- Has its own migration file because Postgres won't let a new enum value
-- be referenced (e.g. in a partial-index WHERE clause) inside the SAME
-- transaction that adds it. apply_migrations.py commits per file, so by
-- the time 011 builds the index, 'scheduled' is committed and usable.

ALTER TYPE request_status ADD VALUE IF NOT EXISTS 'scheduled' AFTER 'approved';
