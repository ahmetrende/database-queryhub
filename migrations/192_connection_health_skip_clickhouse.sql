-- The connection check skips ClickHouse by default.
--
-- Migration 191 seeded `connection_health_exclude_engines` empty, so a fresh
-- install checked every engine. A ClickHouse check is a query. It wakes an
-- idle ClickHouse Cloud service and bills its compute, which is why the
-- catalog job reads ClickHouse once a day. The default now matches the
-- cost: skip `clickhouse`.
--
-- Only a key that still holds the empty seed changes. A key that an operator
-- set to any other value keeps it. An operator who wants ClickHouse checked
-- empties the key on the System configuration screen after this migration
-- ran. A second run finds a non-empty value and changes nothing.

UPDATE bot_config
   SET value = 'clickhouse',
       description = replace(description, 'Empty = check all engines.',
                             'Default: clickhouse. Empty = check all engines.')
 WHERE key = 'connection_health_exclude_engines'
   AND value = '';
