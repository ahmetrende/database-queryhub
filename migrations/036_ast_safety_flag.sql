-- Feature flag for the AST-based second-pass safety layer
-- (src/queryhub/ast_safety.py). Default on — every new install
-- gets the extra defense automatically. Flip to 'off' from psql /
-- bot_config UPDATE if a legitimate query is being mistakenly
-- blocked by sqlglot's parser (rare; the regex layer is still
-- active either way).

INSERT INTO bot_config (key, value, description) VALUES
    ('ast_safety_enabled', 'on',
     'When "on", every submitted query goes through a second-pass AST '
     'check (sqlglot, PostgreSQL dialect) on top of the existing regex / '
     'sqlparse layer. Catches dangerous Postgres-specific functions '
     '(pg_read_file, lo_export, dblink, etc.), COPY ... PROGRAM, long '
     'pg_sleep, and parse-time obfuscation. Set to "off" only to '
     'unblock a legitimate exotic-syntax query.')
ON CONFLICT (key) DO NOTHING;
