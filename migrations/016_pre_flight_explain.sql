-- Pre-flight EXPLAIN: validate the user's query against the actual target
-- (parser + analyzer + planner, no execution) before storing the request
-- and DM'ing admins. Catches: typos that pass safety_analyzer, references
-- to missing tables/columns, permission issues. Adds ~50–300ms to modal
-- submission against an RDS in the same VPC; longer for cross-region.
--
-- Two knobs:
--   pre_flight_explain       ('on'|'off', default 'on')  — run EXPLAIN at all
--   query_plan_logging       ('on'|'off', default 'off') — store the plan
--                                                          JSON in requests

INSERT INTO bot_config (key, value, description) VALUES
    ('pre_flight_explain', 'on',
     'Run EXPLAIN against the target on modal submit to catch syntax / permission / missing-relation errors before admins see the request. "off" skips the check (bot will surface the same errors at execution time instead).'),
    ('query_plan_logging', 'off',
     'When pre_flight_explain is "on" AND this is "on", store the EXPLAIN (FORMAT JSON) output in requests.explain_plan for audit / debugging. Off by default to keep the requests table lean.')
ON CONFLICT (key) DO NOTHING;

-- Persist plans here when query_plan_logging=on. NULL when not captured.
ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS explain_plan JSONB;

COMMENT ON COLUMN requests.explain_plan IS
$$EXPLAIN (FORMAT JSON, COSTS ON) plan captured at modal submit time, when bot_config.query_plan_logging = 'on'. NULL otherwise. Useful for cost analysis / debugging slow queries — not required for execution.$$;
