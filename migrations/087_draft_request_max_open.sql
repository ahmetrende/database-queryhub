-- Seed the cap that reserve_request_id() reads.
--
-- Caught by tests/test_config_keys_seeded.py, which is exactly its job: a key
-- the code reads but no migration seeds is invisible in the admin config UI, so
-- an operator cannot see or change it and only finds out it exists by reading
-- the source. Its own migration rather than an edit to 086, because 086 is
-- committed and the ledger checksums it.
INSERT INTO bot_config (key, value, description) VALUES
  ('draft_request_max_open', '50',
   'Maximum reserved-but-unsubmitted draft requests per user. Reaching it '
   'reaps that user''s oldest drafts rather than refusing a new query tab.')
ON CONFLICT (key) DO NOTHING;
