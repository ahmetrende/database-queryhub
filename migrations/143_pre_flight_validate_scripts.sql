-- Submit-time validation now covers every statement of a request.
--
-- Before this, the pre-flight EXPLAIN planned one read-only statement. It
-- skipped every write and every script with more than one statement. A wrong
-- column in an UPDATE, or a typo in the third SELECT of a script, reached an
-- approver. The approver approved it, and the requester saw the error only when
-- it ran. Over 90 days, 65 requests failed after approval with an error of this
-- kind. In 61 of them the same person ran a corrected query on the same target
-- within a day.
--
-- The new check plans each statement with EXPLAIN in one read-only
-- transaction. It runs nothing. It refuses a mistake at submit, with the number
-- of the statement that failed. It never blocks on a problem of its own: an
-- unreachable target, a timeout or a lock wait lets the request through.
--
-- `pre_flight_validate_scripts` is the switch. It needs `pre_flight_explain`
-- on. Set it to `off` to go back to the old behaviour at once, with no restart.
--
-- The default is `on`, the same value the code passes. A second run changes
-- nothing, and an install that set the key by hand keeps its value.

INSERT INTO bot_config (key, value, description) VALUES
    ('pre_flight_validate_scripts', 'on',
     'At submit time, plan every statement of a write request and of a multi-statement script with EXPLAIN. Refuse a mistake before an approver sees it. Nothing runs. Needs pre_flight_explain on. "off" checks one read-only statement only.')
ON CONFLICT (key) DO NOTHING;
