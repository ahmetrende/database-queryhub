-- 208: cap the parallel items of one fleet run (2026-11-12).
--
-- A fleet run that continues after a failure ran every item on the shared
-- pool of 4 workers, so a large run blocked every other request until it
-- ended. The executor now runs at most this many items of one fleet run at
-- the same time. The code clamps the value to 1..3, so one worker always
-- stays free. Safe to run again.

INSERT INTO bot_config (key, value, description) VALUES
    ('fleet_max_parallel', '2',
     'Maximum items of one fleet run that run at the same time (1 to 3). A run that stops at the first failure always runs one item at a time.')
ON CONFLICT (key) DO NOTHING;
