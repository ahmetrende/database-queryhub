-- Master kill switch.
--
-- Set bot_config.kill_switch = 'on' to immediately block:
--   - new /sql submissions (modal open)
--   - new modal submission (in case a user had it open already)
--   - access-request submissions
--   - scheduler dispatch of due scheduled requests (already-scheduled rows
--     stay in the queue; they fire whenever kill switch goes back to off)
--
-- Doesn't block:
--   - admin Approve/Reject/Cancel on existing requests (so DBA can drain
--     the pending queue cleanly during a maintenance window)
--   - already-executing queries (no programmatic interrupt)
--   - read-only inspection by the DBA via DataGrip
--
-- Toggle (no restart needed — bot reads on every relevant action):
--   UPDATE bot_config SET value = 'on'  WHERE key = 'kill_switch';   -- block
--   UPDATE bot_config SET value = 'off' WHERE key = 'kill_switch';   -- restore

INSERT INTO bot_config (key, value, description) VALUES
    ('kill_switch', 'off',
     'Master kill switch. "on" blocks new submissions and scheduler dispatch; admin approve/reject still work for cleanup. Set to "off" to resume normal operation.'),
    ('kill_switch_message',
     ':construction: The SQL bot is temporarily disabled. Please try again later or contact the DBA team.',
     'Ephemeral message shown to users when kill_switch is on. Edit to communicate downtime reason / ETA.')
ON CONFLICT (key) DO NOTHING;
