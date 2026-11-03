-- 204: audit the switches of the MCP door like the other security keys.
--
-- `mcp_door_enabled` lets a program act for people, `mcp_max_tier` lets it
-- submit writes, and `mcp_result_mode` decides whether result rows reach the
-- model. A change of one of them is a change of what data leaves QueryHub, so
-- the trigger of migrations 056 and 066 now records it in audit_log
-- (`security_config_changed`, `security_config_deleted`), as it records the
-- kill switch. The function is the one of migration 066 with three more keys.
-- A later migration that replaces it must keep them.
--
-- Idempotent: CREATE OR REPLACE FUNCTION. The trigger of migration 066 stays.

CREATE OR REPLACE FUNCTION bot_config_audit() RETURNS trigger AS $$
DECLARE
  security_keys text[] := ARRAY[
    'ast_safety_enabled', 'kill_switch', 'pii_masking_enabled', 'pre_flight_explain',
    'mcp_door_enabled', 'mcp_max_tier', 'mcp_result_mode'
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
