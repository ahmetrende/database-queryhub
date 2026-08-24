-- What the server said while the query ran.
--
-- A DDL script answers with NOTICE lines and nothing else: the role script
-- that prompted this raises "Role created" or "Role already exists", and that
-- sentence is the only difference between the two branches. QueryHub dropped
-- every notice, so a nine-statement run reported "0 rows" and looked like it
-- had done nothing. It was run twice for that reason (requests 5096, 5097).
--
-- Shape: {"statements": [{"i": 1, "leading": "DO", "rows": 0}, ...],
--         "notices":    [{"i": 1, "severity": "NOTICE", "text": "..."}, ...],
--         "truncated":  false}
-- Read by the web status feed and rendered in the Messages tab.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS run_notes jsonb;

COMMENT ON COLUMN requests.run_notes IS
  'Per-run server output: statement summary + captured NOTICE/WARNING lines.';
