-- The switch that decides which model answers an authorization question.
--
-- `teams.py` keeps its signatures and has two bodies behind each: the tables it
-- has always read, and a delegation to `access.py` on the nine-table model.
-- This key chooses. Read per call like every other runtime setting, so turning
-- it back is a config change rather than a deploy — which is the rollback for
-- the whole cutover.
--
-- OFF here on purpose. It may only be turned on when
-- `scripts/access_snapshot.py compare` reports no difference between the two
-- resolvers over every (principal, target, database) answer in the fleet. That
-- comparison is the evidence; nothing else is.
--
-- Seeded rather than left to the code's default so the admin config screen can
-- show and set it, which is what tests/test_config_keys_seeded.py enforces for
-- every key the code reads.
INSERT INTO bot_config (key, value, description) VALUES
  ('access_model_v2', 'off',
   'Resolve authorization from the nine-table model (access_grant, '
   'role_assignment) instead of the team tables. Only turn on when the access '
   'snapshot comparison is empty.')
ON CONFLICT (key) DO NOTHING;
