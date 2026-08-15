-- =============================================================================
-- QueryHub — bot-DB admin templates (IDE-agnostic, psql variable style)
-- =============================================================================
-- Manage the bot's metadata DB directly via SQL — admins, target servers,
-- bot_config. The CLI scripts that used to do this have been removed; this
-- file is the canonical surface.
--
-- The `:'name'` syntax works in:
--   - psql (CLI)            : pass values with -v name=value
--   - DataGrip / DataSpell  : highlight block + run, dialog prompts for params
--   - DBeaver / IntelliJ    : same as DataGrip (JDBC named-param binding)
--
-- Connect as the slackbot user against the slackbot database.
--
--   psql -h <bot-db-host> -U slackbot -d slackbot \
--        -v slack_user_id=U0NEW01 -v admin_name='Surname Name' \
--        -f deploy/db_admin_templates.sql
--
-- For the target_servers password, see deploy/team_admin_templates.sql header
-- and the encrypt_secret.py helper:
--
--   .venv/bin/python scripts/encrypt_secret.py
--   # paste the output as :password_encrypted in the INSERT below.
-- =============================================================================


-- =============================================================================
-- REQUESTERS  (allowlist of users who can invoke /sql)
-- =============================================================================
-- Empty table = bot is OPEN to all workspace users (kill-switch off).
-- Any enabled rows = only those users may run /sql. Admins always pass
-- regardless of this table.
-- Email is filled in lazily by the bot via Slack users.info on the user's
-- first interaction; you don't need to supply it here.

-- Add (or re-enable) a requester
INSERT INTO requesters (slack_user_id, name, added_by)
VALUES (:'slack_user_id', NULLIF(:'requester_name',''), :'added_by_slack_id')
ON CONFLICT (slack_user_id) DO UPDATE
    SET enabled = TRUE,
        name    = COALESCE(EXCLUDED.name, requesters.name)
RETURNING slack_user_id, name, email, enabled, added_at;


-- Disable (keep row for audit)
UPDATE requesters SET enabled = FALSE
WHERE slack_user_id = :'slack_user_id'
RETURNING slack_user_id, name, email, enabled;


-- Re-enable
UPDATE requesters SET enabled = TRUE
WHERE slack_user_id = :'slack_user_id'
RETURNING slack_user_id, name, email, enabled;


-- Hard delete (rare — usually you just disable)
DELETE FROM requesters WHERE slack_user_id = :'slack_user_id'
RETURNING slack_user_id;


-- List enabled
SELECT slack_user_id, name, email, added_at, added_by
FROM requesters WHERE enabled = TRUE ORDER BY added_at;

-- List all
SELECT slack_user_id, name, email, enabled, added_at, added_by
FROM requesters ORDER BY enabled DESC, added_at;


-- =============================================================================
-- ADMINS
-- =============================================================================

-- Add an admin (email autofills via Slack on first interaction)
INSERT INTO admins (slack_user_id, name, added_by)
VALUES (:'slack_user_id', :'admin_name', :'added_by_slack_id')
ON CONFLICT (slack_user_id) DO UPDATE
    SET name = EXCLUDED.name, enabled = TRUE
RETURNING slack_user_id, name, email, enabled, added_at;


-- Disable an admin (keep audit trail; do not delete)
UPDATE admins SET enabled = FALSE
WHERE slack_user_id = :'slack_user_id'
RETURNING slack_user_id, name, email, enabled;


-- Re-enable an admin
UPDATE admins SET enabled = TRUE
WHERE slack_user_id = :'slack_user_id'
RETURNING slack_user_id, name, email, enabled;


-- List active admins
SELECT slack_user_id, name, email, added_at, added_by
FROM admins WHERE enabled = TRUE ORDER BY added_at;


-- List all admins (incl. disabled)
SELECT slack_user_id, name, email, enabled, added_at, added_by
FROM admins ORDER BY enabled DESC, added_at;


-- =============================================================================
-- TARGET SERVERS
-- =============================================================================

-- Add a target server (RO credentials).
--
-- :password_encrypted MUST be the Fernet ciphertext produced by:
--     .venv/bin/python scripts/encrypt_secret.py
-- Plaintext passwords are NEVER inserted directly.
--
INSERT INTO target_servers (alias, host, port, default_database, username,
                            password_encrypted, notes)
VALUES (
    :'alias',
    :'host',
    :'port'::int,
    :'default_database',
    :'username',
    :'password_encrypted',
    NULLIF(:'notes', '')
)
RETURNING id, alias, host, port, default_database, username, enabled;


-- Update only the RO password for an existing target (rotation).
-- Re-run encrypt_secret.py with the new password, paste here.
UPDATE target_servers
SET password_encrypted = :'password_encrypted',
    updated_at = NOW()
WHERE id = :'target_server_id'::int
RETURNING id, alias;


-- Set / rotate the RW credentials for a target. Bot uses these when the
-- query is INSERT/UPDATE/DELETE/MERGE/COPY AND the requester has a grant
-- with mode='rw' or 'ddl'. Pass empty :rw_username + :password_encrypted_rw
-- to UN-configure RW (write queries on this target then fail-fast).
UPDATE target_servers
SET username_rw = NULLIF(:'rw_username', ''),
    password_rw_encrypted = NULLIF(:'password_encrypted_rw', ''),
    updated_at = NOW()
WHERE id = :'target_server_id'::int
RETURNING id, alias, username_rw IS NOT NULL AS rw_configured;


-- Set / rotate DDL credentials (CREATE/ALTER/DROP/TRUNCATE/VACUUM/etc.).
UPDATE target_servers
SET username_ddl = NULLIF(:'ddl_username', ''),
    password_ddl_encrypted = NULLIF(:'password_encrypted_ddl', ''),
    updated_at = NOW()
WHERE id = :'target_server_id'::int
RETURNING id, alias, username_ddl IS NOT NULL AS ddl_configured;


-- Disable a target (hides from /sql but keeps row + creds for re-enable)
UPDATE target_servers SET enabled = FALSE, updated_at = NOW()
WHERE id = :'target_server_id'::int
RETURNING id, alias, enabled;


-- Enable a target
UPDATE target_servers SET enabled = TRUE, updated_at = NOW()
WHERE id = :'target_server_id'::int
RETURNING id, alias, enabled;


-- Update connection metadata (host/port/db/notes — NOT the password)
UPDATE target_servers
SET host             = :'host',
    port             = :'port'::int,
    default_database = :'default_database',
    username         = :'username',
    notes            = NULLIF(:'notes', ''),
    updated_at       = NOW()
WHERE id = :'target_server_id'::int
RETURNING id, alias, host, port, default_database, username;


-- Delete a target permanently (will cascade-drop team_target_grants rows
-- referencing it, so re-grant after re-adding if needed).
DELETE FROM target_servers WHERE id = :'target_server_id'::int
RETURNING id, alias;


-- Inspections
SELECT id, alias, host, port, default_database,
       username                                                  AS user_ro,
       password_encrypted     IS NOT NULL                        AS ro_set,
       username_rw                                               AS user_rw,
       password_rw_encrypted  IS NOT NULL                        AS rw_set,
       username_ddl                                              AS user_ddl,
       password_ddl_encrypted IS NOT NULL                        AS ddl_set,
       enabled, notes, created_at, updated_at
FROM target_servers ORDER BY alias;

SELECT id, alias FROM target_servers WHERE enabled = TRUE ORDER BY alias;


-- =============================================================================
-- ACCESS / PERMISSIONS — full overview
-- =============================================================================
-- These three SELECTs answer "who has what access?" at three different
-- granularities. They cover admins, requesters, bypass flag, team grants,
-- and user-level overrides — everything the bot considers at runtime.


-- ---------- A. Per-user summary (one row per user with flags + counts) ----------
WITH all_users AS (
    SELECT slack_user_id FROM admins      WHERE enabled
    UNION SELECT slack_user_id FROM requesters WHERE enabled
)
SELECT
    u.slack_user_id,
    COALESCE(a.name, r.name)                    AS name,
    COALESCE(a.email, r.email)                  AS email,
    (a.slack_user_id IS NOT NULL)               AS is_admin,
    (r.slack_user_id IS NOT NULL AND r.enabled) AS in_allowlist,
    COALESCE(r.bypass_team_grants, FALSE)       AS bypass_team_grants,
    -- target_count: admins/bypass see all enabled targets; others see only granted
    CASE
        WHEN a.slack_user_id IS NOT NULL OR COALESCE(r.bypass_team_grants, FALSE)
            THEN (SELECT count(*) FROM target_servers WHERE enabled)
        ELSE (SELECT count(*) FROM v_effective_user_grants
              WHERE slack_user_id = u.slack_user_id)
    END AS target_count,
    -- max_mode: highest tier they can hit anywhere
    CASE
        WHEN a.slack_user_id IS NOT NULL OR COALESCE(r.bypass_team_grants, FALSE)
            THEN 'ddl'
        ELSE COALESCE(
            (SELECT max(mode::text) FROM v_effective_user_grants
             WHERE slack_user_id = u.slack_user_id
               AND mode IN ('ro','rw','ddl')),
            '(none)'
        )
    END AS max_mode
FROM all_users u
LEFT JOIN admins     a ON a.slack_user_id = u.slack_user_id AND a.enabled
LEFT JOIN requesters r ON r.slack_user_id = u.slack_user_id
ORDER BY is_admin DESC NULLS LAST, name NULLS LAST;


-- ---------- B. Detail: each (user, target) the user can reach ----------
-- Admins + bypass requesters are synthesized as having ddl-on-everything.
SELECT
    coalesce(r.name, a.name, '(?)')                              AS name,
    g.slack_user_id,
    ts.alias                                                     AS target,
    g.mode,
    coalesce(array_to_string(g.allowed_databases, ', '), '(all dbs)') AS databases,
    g.source
FROM (
    SELECT slack_user_id, target_server_id, mode, allowed_databases, source
    FROM v_effective_user_grants
    UNION ALL
    SELECT a.slack_user_id, ts.id, 'ddl', NULL, 'admin'
    FROM admins a CROSS JOIN target_servers ts
    WHERE a.enabled AND ts.enabled
    UNION ALL
    SELECT r.slack_user_id, ts.id, 'ddl', NULL, 'bypass'
    FROM requesters r CROSS JOIN target_servers ts
    WHERE r.enabled AND r.bypass_team_grants AND ts.enabled
      AND NOT EXISTS (
          SELECT 1 FROM admins WHERE slack_user_id = r.slack_user_id AND enabled)
) g
JOIN target_servers ts  ON ts.id = g.target_server_id
LEFT JOIN requesters r  ON r.slack_user_id = g.slack_user_id
LEFT JOIN admins a      ON a.slack_user_id = g.slack_user_id
ORDER BY name, target;


-- ---------- C. Reverse: who can reach a given target ----------
-- Pass :target_alias = e.g. 'acme-prod-orders'.
SELECT
    ts.alias                                              AS target,
    coalesce(r.name, a.name, '(?)')                       AS user_name,
    g.slack_user_id,
    g.mode,
    coalesce(array_to_string(g.allowed_databases, ', '), '(all dbs)') AS databases,
    g.source
FROM target_servers ts
LEFT JOIN LATERAL (
    SELECT slack_user_id, mode, allowed_databases, source
    FROM v_effective_user_grants WHERE target_server_id = ts.id
    UNION ALL
    SELECT a.slack_user_id, 'ddl', NULL, 'admin' FROM admins a WHERE a.enabled
    UNION ALL
    SELECT r.slack_user_id, 'ddl', NULL, 'bypass' FROM requesters r
    WHERE r.enabled AND r.bypass_team_grants
      AND NOT EXISTS (
          SELECT 1 FROM admins WHERE slack_user_id = r.slack_user_id AND enabled)
) g ON TRUE
LEFT JOIN requesters r ON r.slack_user_id = g.slack_user_id
LEFT JOIN admins     a ON a.slack_user_id = g.slack_user_id
WHERE ts.enabled AND g.slack_user_id IS NOT NULL
  AND ts.alias = :'target_alias'
ORDER BY user_name;


-- =============================================================================
-- BOT_CONFIG (runtime tunables — change without restart)
-- =============================================================================

-- Read all current settings
SELECT key, value, description, updated_at
FROM bot_config ORDER BY key;


-- Read one setting
SELECT key, value, description FROM bot_config WHERE key = :'config_key';


-- Update a setting (or insert if missing)
INSERT INTO bot_config (key, value, description)
VALUES (:'config_key', :'config_value', :'config_description')
ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value,
        description = COALESCE(EXCLUDED.description, bot_config.description),
        updated_at = NOW()
RETURNING key, value, updated_at;


-- Common adjustments (uncomment + run as needed):
-- UPDATE bot_config SET value = '500',  updated_at = NOW() WHERE key = 'max_rows';
-- UPDATE bot_config SET value = '600',  updated_at = NOW() WHERE key = 'query_timeout_sec';
-- UPDATE bot_config SET value = 'true', updated_at = NOW() WHERE key = 'require_justification';


-- =============================================================================
-- KILL SWITCH (master halt for the whole bot)
-- =============================================================================
-- Effect when ON:
--   - new /sql submissions blocked (modal still opens, gets a downtime
--     ephemeral instead of the form)
--   - in-progress modal submissions hit a downtime error
--   - access-request submissions blocked
--   - scheduler stops dispatching due 'scheduled' rows (they queue up)
-- Effect on what's already in flight:
--   - already-running queries finish normally
--   - admin Approve/Reject/Cancel still work (drain pending queue)


-- ---------- Turn the bot OFF ----------
UPDATE bot_config SET value = 'on', updated_at = NOW() WHERE key = 'kill_switch';
-- Optionally set a custom message (Slack mrkdwn supported):
UPDATE bot_config SET value = :'kill_message', updated_at = NOW()
WHERE key = 'kill_switch_message';


-- ---------- Turn the bot ON again ----------
UPDATE bot_config SET value = 'off', updated_at = NOW() WHERE key = 'kill_switch';


-- ---------- Inspect current state ----------
SELECT key, value, updated_at
FROM bot_config
WHERE key IN ('kill_switch', 'kill_switch_message');


-- =============================================================================
-- PRE-FLIGHT EXPLAIN (modal-submit time validation)
-- =============================================================================
-- pre_flight_explain  ('on'|'off', default 'on')  — run EXPLAIN against the
--                                                    target before saving.
-- query_plan_logging  ('on'|'off', default 'off') — store the plan JSON in
--                                                    requests.explain_plan.

UPDATE bot_config SET value = 'off', updated_at = NOW() WHERE key = 'pre_flight_explain';
UPDATE bot_config SET value = 'on',  updated_at = NOW() WHERE key = 'pre_flight_explain';

UPDATE bot_config SET value = 'off', updated_at = NOW() WHERE key = 'query_plan_logging';
UPDATE bot_config SET value = 'on',  updated_at = NOW() WHERE key = 'query_plan_logging';

-- View captured plans for recent requests (latest first)
SELECT id, requester_slack_id, target_server_id, database_name,
       jsonb_array_length(COALESCE(explain_plan, '[]'::jsonb)) AS plan_nodes,
       (explain_plan -> 0 -> 'Plan' ->> 'Total Cost')::float    AS total_cost,
       (explain_plan -> 0 -> 'Plan' ->> 'Plan Rows')::bigint    AS est_rows,
       created_at
FROM requests
WHERE explain_plan IS NOT NULL
ORDER BY id DESC LIMIT 50;

-- Pull a single plan in pretty-printed form
SELECT jsonb_pretty(explain_plan)
FROM requests WHERE id = :'request_id'::bigint;


-- =============================================================================
-- AUDIT INSPECTIONS
-- =============================================================================

-- Recent requests with status + timing
SELECT id, requester_slack_id, requester_name, status,
       target_server_id, database_name,
       row_count, truncated, error_message,
       created_at, completed_at
FROM requests ORDER BY id DESC LIMIT 50;


-- Audit trail for one request
SELECT id, actor_slack_id, actor_name, action, details, created_at
FROM audit_log WHERE request_id = :'request_id'::bigint ORDER BY created_at;


-- Most recent failures
SELECT id, requester_slack_id, status, error_message, created_at
FROM requests
WHERE status IN ('failed', 'rejected')
ORDER BY id DESC LIMIT 20;
