-- How many tables one database contributes to the web /connections payload.
--
-- This used to be a hardcoded 200 that truncated silently: the largest
-- database in the fleet has ~2.7k relations, so ~2.5k of them were absent
-- from the schema tree AND from editor autocomplete with nothing on screen
-- saying the list was partial. People concluded the table "wasn't there".
-- Configurable now, with a ceiling high enough to cover every database we
-- actually have, and the payload reports when it did truncate.

INSERT INTO bot_config (key, value, description) VALUES
('web_max_tables_per_db', '2000',
 'Maximum tables one database contributes to the web /connections payload. Beyond this the list is truncated and flagged; raise it if a database legitimately has more.')
ON CONFLICT (key) DO NOTHING;
