-- The four settings of the periodic connection check (migration 190,
-- scripts/check_connection_health.py).
--
-- The script reads them at the start of each run, so a change needs no edit to
-- the scheduled job and no restart. The defaults are the values the code
-- passes. A second run changes nothing, and an install that set a key by hand
-- keeps its value.
--
-- The check covers every engine by default (operator decision, 2026-10-22).
-- The exclusion key exists for the cost: a ClickHouse check is a query, and a
-- query wakes an idle ClickHouse Cloud service and bills its compute. That is
-- why the catalog job reads ClickHouse once a day.

INSERT INTO bot_config (key, value, description) VALUES
    ('connection_health_enabled', 'on',
     'on = a scheduled job checks the connection to each enabled target, and the Connections screen shows the last result. off = no check runs, and the screen shows no result.'),
    ('connection_health_interval_sec', '300',
     'Seconds between two checks of the same target. The scheduled job can run more often. It checks only the targets that are due.'),
    ('connection_health_timeout_sec', '5',
     'Seconds that one check can take, from 1 to 60. After this time, the check records a failure.'),
    ('connection_health_exclude_engines', '',
     'Engines that the check skips, as a comma list: postgres, mssql, clickhouse, athena. Empty = check all engines. Cost: a ClickHouse check is a query. It wakes an idle ClickHouse Cloud service and bills its compute. That is why the catalog job reads ClickHouse once a day. An Athena check reads the Glue catalog and runs no Athena query.')
ON CONFLICT (key) DO NOTHING;
