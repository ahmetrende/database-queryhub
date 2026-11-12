-- 207: fleet runs (2026-11-12).
--
-- A fleet run is one SQL text sent to many (target, database) pairs as ONE
-- bundle. The bundle row records its kind and what to do when an item fails.
-- 'batch' is the old behavior and stays the default.
-- Safe to run again.

ALTER TABLE request_bundles
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'batch';

ALTER TABLE request_bundles
    ADD COLUMN IF NOT EXISTS on_failure text;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'request_bundles_kind_check'
                      AND conrelid = 'request_bundles'::regclass) THEN
        ALTER TABLE request_bundles
            ADD CONSTRAINT request_bundles_kind_check
            CHECK (kind IN ('batch', 'fleet'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'request_bundles_on_failure_check'
                      AND conrelid = 'request_bundles'::regclass) THEN
        ALTER TABLE request_bundles
            ADD CONSTRAINT request_bundles_on_failure_check
            CHECK (on_failure IS NULL OR on_failure IN ('continue', 'stop'));
    END IF;
END
$$;

INSERT INTO bot_config (key, value, description) VALUES
    ('fleet_max_targets', '50',
     'Maximum (target, database) pairs in one fleet run. A fleet run is one SQL text sent to many databases as one bundle with one approval.')
ON CONFLICT (key) DO NOTHING;
