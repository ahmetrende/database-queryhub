-- Static risk hints for admin review (Smart Routing 3a).
--
-- The pre-flight EXPLAIN already runs at submission time; this migration
-- lets us persist a short, render-ready risk summary derived from that
-- plan so the admin DM can show "what will this query do" at a glance —
-- sequential scans on large tables, high planner cost, estimated row
-- count. NOT auto-approval: the human still clicks Approve. The summary
-- is informational only.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS risk_summary TEXT;

COMMENT ON COLUMN requests.risk_summary IS
$$Render-ready risk hint string derived from the pre-flight EXPLAIN plan
(seq scans, planner cost, estimated rows). Shown in the admin DM to
inform the approval decision. NULL when the plan was unavailable
(DDL / transport fail-open / pre-flight disabled).$$;

-- Thresholds an operator can tune without a redeploy. A seq scan whose
-- estimated row count exceeds risk_seq_scan_rows, or a plan whose total
-- cost exceeds risk_high_cost, gets flagged with a warning glyph.
INSERT INTO bot_config (key, value, description) VALUES
    ('risk_seq_scan_rows', '100000',
     'Sequential scans estimated to read more than this many rows get a warning hint in the admin DM.'),
    ('risk_high_cost', '50000',
     'Pre-flight EXPLAIN total cost above this value gets a warning hint in the admin DM.')
ON CONFLICT (key) DO NOTHING;
