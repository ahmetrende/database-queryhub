-- One SQL Server statement can send more than one result set, for example a
-- trigger that returns rows. The executor keeps each set as a table of its own
-- (executor._more_mssql_results). This key caps the tables per statement. The
-- code reads it with the same default.
INSERT INTO bot_config (key, value, description) VALUES
    ('max_result_sets', '20',
     'Most tables that one SQL Server statement can produce. Later result sets are read and dropped, and the Messages tab says how many.')
ON CONFLICT (key) DO NOTHING;
