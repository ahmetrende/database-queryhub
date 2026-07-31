-- Audit DELETEs of security-relevant bot_config keys (SEC-18).
--
-- migration 056 audits INSERT/UPDATE of the security key set, but the
-- trigger fires only BEFORE INSERT OR UPDATE. A `DELETE FROM bot_config
-- WHERE key = 'kill_switch'` therefore removes the row with NO audit trail —
-- and because config reads fall back to a hard-coded default when a key is
-- absent (kill_switch -> 'off', ast_safety_enabled -> 'on', ...), deleting
-- the kill_switch row silently DISABLES the kill switch. This closes that
-- gap: the audit function now also handles DELETE (logging the old value
-- before the row disappears) and the trigger fires on DELETE too.
--
-- Idempotent: CREATE OR REPLACE FUNCTION + DROP/CREATE TRIGGER.

CREATE OR REPLACE FUNCTION bot_config_audit() RETURNS trigger AS $$
DECLARE
  security_keys text[] := ARRAY[
    'ast_safety_enabled', 'kill_switch', 'pii_masking_enabled', 'pre_flight_explain'
  ];
BEGIN
  IF TG_OP = 'DELETE' THEN
    -- Deleting a security key drops it back to its code default with no
    -- record of who removed it. Log it before the row is gone.
    IF OLD.key = ANY(security_keys) THEN
      INSERT INTO audit_log (request_id, actor_slack_id, actor_name, action, details)
      VALUES (
        NULL, NULL, 'db:' || current_user, 'security_config_deleted',
        jsonb_build_object(
          'key', OLD.key,
          'old_value', OLD.value,
          'new_value', NULL,
          'db_user', current_user,
          'application_name', current_setting('application_name', true)
        )
      );
    END IF;
    RETURN OLD;
  END IF;

  -- INSERT / UPDATE: keep updated_at honest for the TTL revert, and record
  -- security-key value changes (unchanged from migration 056).
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
  BEFORE INSERT OR UPDATE OR DELETE ON bot_config
  FOR EACH ROW EXECUTE FUNCTION bot_config_audit();
