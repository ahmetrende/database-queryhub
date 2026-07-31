-- Audit + TTL for security-relevant bot_config toggles.
--
-- Two gaps this closes:
--   1. A single `UPDATE bot_config SET value='off' WHERE key='ast_safety_enabled'`
--      silently disables a safety layer with no record of who or when.
--   2. Nothing re-arms it; a toggle flipped "just to debug" stays off.
--
-- (1) is handled here by a BEFORE trigger that stamps updated_at and,
-- for the security key set, writes an audit_log row capturing old/new
-- value, the DB role, and the application_name. It catches raw-SQL
-- changes, which is exactly how these toggles get flipped.
--
-- (2) is handled by scripts/revert_security_overrides.py, which re-enables
-- any fail-secure toggle left 'off' past security_override_ttl_minutes.
-- The timestamp it reads is the updated_at this trigger maintains.

CREATE OR REPLACE FUNCTION bot_config_audit() RETURNS trigger AS $$
DECLARE
  security_keys text[] := ARRAY[
    'ast_safety_enabled', 'kill_switch', 'pii_masking_enabled', 'pre_flight_explain'
  ];
BEGIN
  -- Always keep updated_at honest so the TTL revert has a real clock.
  NEW.updated_at := now();
  IF NEW.key = ANY(security_keys)
     AND (TG_OP = 'INSERT' OR NEW.value IS DISTINCT FROM OLD.value) THEN
    INSERT INTO audit_log (request_id, actor_slack_id, actor_name, action, details)
    VALUES (
      NULL, NULL, 'db:' || current_user, 'security_config_changed',
      jsonb_build_object(
        'key', NEW.key,
        'old_value', CASE WHEN TG_OP = 'UPDATE' THEN OLD.value ELSE NULL END,
        'new_value', NEW.value,
        'db_user', current_user,
        'application_name', current_setting('application_name', true)
      )
    );
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bot_config_audit ON bot_config;
CREATE TRIGGER trg_bot_config_audit
  BEFORE INSERT OR UPDATE ON bot_config
  FOR EACH ROW EXECUTE FUNCTION bot_config_audit();

INSERT INTO bot_config (key, value, description) VALUES
  ('security_override_ttl_enabled', 'off',
   'When on, revert_security_overrides.py --commit re-arms fail-secure toggles left off past the TTL.'),
  ('security_override_ttl_minutes', '60',
   'A fail-secure toggle (ast_safety_enabled / pii_masking_enabled / pre_flight_explain) left off longer than this is auto-reverted to on.')
ON CONFLICT (key) DO NOTHING;
