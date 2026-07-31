-- Pending-request auto-expiry config.
--
-- A request that sits in 'pending' (awaiting admin action) forever is
-- noise in the queue and a stale grant of intent. scripts/
-- expire_stale_pending.py cancels pending requests older than
-- pending_expiry_hours. Like the grant reaper, it is dry-run by default
-- and only acts under --commit when pending_expiry_enabled is 'on', so a
-- daily job can run --commit harmlessly until an operator opts in.
--
-- Expired requests move to 'cancelled' (an existing terminal state that
-- every render / metrics path already handles) with a decision_reason of
-- 'expired ...'; the forensic distinction lives in audit_log under the
-- dedicated action 'request_expired'. Approve/Reject buttons on the stale
-- admin DM are already no-ops once status != 'pending'.
INSERT INTO bot_config (key, value, description) VALUES
  ('pending_expiry_enabled', 'off',
   'When on, expire_stale_pending.py --commit cancels stale pending requests.'),
  ('pending_expiry_hours', '24',
   'A pending request older than this many hours is auto-expired (cancelled).')
ON CONFLICT (key) DO NOTHING;
