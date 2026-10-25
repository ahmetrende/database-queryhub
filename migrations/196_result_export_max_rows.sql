-- The most rows that a JSON, Markdown or SQL INSERT download can hold.
--
-- The web offers a finished result in more shapes than CSV and Excel. Each of
-- these downloads reads the stored, already masked CSV and writes the new shape
-- while it reads. A very large result makes a very large file that nobody reads
-- as Markdown. So a result with more rows than this answers with an error and
-- tells the person to download the CSV.
--
-- The application reads the key on each download, so a change needs no
-- restart. A second run changes nothing, and an install that set the key by
-- hand keeps its value.

INSERT INTO bot_config (key, value, description) VALUES
    ('result_export_max_rows', '100000',
     'Most rows that a JSON, Markdown or SQL INSERT download can hold. A bigger result answers with an error and the person downloads the CSV instead. A whole number of 1 or more.')
ON CONFLICT (key) DO NOTHING;
