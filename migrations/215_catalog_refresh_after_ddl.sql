-- After a DDL request completes, QueryHub refreshes the schema catalog of that
-- request's target and database (schema_catalog.refresh_after_ddl). This key
-- turns the refresh off. The code reads it with the same default.
INSERT INTO bot_config (key, value, description) VALUES
    ('catalog_refresh_after_ddl', 'on',
     'on | off. After a DDL request completes, refresh the schema catalog of its target and database in the background, with the RO credential.')
ON CONFLICT (key) DO NOTHING;
