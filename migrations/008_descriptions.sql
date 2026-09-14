-- Annotate every object with a description. Same prose lives in
-- docs/SCHEMA.md; this migration mirrors it into pg_description so DataGrip /
-- psql `\d+ <object>` shows the same context.
--
-- Idempotent: COMMENT ON statements overwrite the previous comment, so
-- running this migration multiple times (or after copy edits) is safe.

-- ============================================================================
-- TABLES
-- ============================================================================

COMMENT ON TABLE bot_config IS
$$Runtime knobs as a key/value table. The bot reads on every relevant request, so changing a value via UPDATE takes effect immediately — no restart needed (exception: log_level, read once at process start). Current keys: max_rows, csv_size_mb, query_timeout_sec, require_justification, min_query_length, bot_display_name, bot_display_icon, log_level, results_ttl_hours.$$;

COMMENT ON COLUMN bot_config.key IS 'Setting name. See docs/SCHEMA.md for the canonical list.';
COMMENT ON COLUMN bot_config.value IS 'String value; bot casts to int/bool/etc. as needed.';
COMMENT ON COLUMN bot_config.description IS 'Free text explaining what the key controls.';

COMMENT ON TABLE target_servers IS
$$Postgres servers the bot can query. One row per (host, default_database, user). Connection passwords are Fernet-encrypted with the on-disk master.key and stored in password_encrypted as ciphertext.$$;

COMMENT ON COLUMN target_servers.alias IS 'Unique short label shown in the /sql modal dropdown.';
COMMENT ON COLUMN target_servers.password_encrypted IS 'Fernet ciphertext. Generate via scripts/encrypt_secret.py then INSERT raw.';
COMMENT ON COLUMN target_servers.enabled IS 'Soft-delete flag — disabled rows disappear from the modal but remain queryable for audit.';
COMMENT ON COLUMN target_servers.notes IS 'Free text — describe purpose, owner, on-call team, etc.';

COMMENT ON TABLE admins IS
$$Slack users authorized to approve/reject /sql requests. Admins ALSO bypass the requester allowlist (requesters table) and team grants (team_target_grants) — they can submit /sql against any target.$$;

COMMENT ON COLUMN admins.slack_user_id IS 'Slack member ID (U… or W…).';
COMMENT ON COLUMN admins.email IS 'Lazily backfilled by the bot from Slack users.info on first interaction.';
COMMENT ON COLUMN admins.enabled IS 'Soft-delete flag.';

COMMENT ON TABLE requesters IS
$$Allowlist of Slack users who may invoke /sql (first authorization layer / kill-switch). Empty table = bot is open to all workspace users. Any enabled rows = only those listed users may run /sql. Admins always pass regardless.$$;

COMMENT ON COLUMN requesters.slack_user_id IS 'Slack member ID, validated by CHECK pattern.';
COMMENT ON COLUMN requesters.email IS 'Lazily backfilled by the bot from Slack users.info on first interaction.';
COMMENT ON COLUMN requesters.enabled IS 'Disable to revoke without losing audit history.';

COMMENT ON TABLE teams IS
$$Logical grouping of users. A team is granted access to one or more targets via team_target_grants; members of the team inherit those grants.$$;

COMMENT ON COLUMN teams.name IS 'Unique team identifier (e.g. "payment", "ingest").';

COMMENT ON TABLE team_members IS
$$Many-to-many: which Slack users belong to which teams. A user can be in multiple teams; their grants are the union of all team grants.$$;

COMMENT ON TABLE team_target_grants IS
$$Which targets (and which databases on each target) a team can reach. allowed_databases NULL or empty = all DBs allowed; non-empty = only those DBs.$$;

COMMENT ON COLUMN team_target_grants.allowed_databases IS 'NULL/empty = all DBs on the target are allowed; non-empty array = only those DBs (case-sensitive match).';

COMMENT ON TABLE requests IS
$$Every /sql submission, regardless of outcome. The audit trail. Joined to request_notifications for chat.update lockstep, and to audit_log for state-transition history.$$;

COMMENT ON COLUMN requests.status IS 'enum request_status: pending → (approved | rejected | changes_requested) → (executing → completed | failed) | cancelled.';
COMMENT ON COLUMN requests.csv_file_path IS 'Local path under /var/lib/slackbot/results/. NULL''d by cleanup after TTL.';
COMMENT ON COLUMN requests.slack_file_id IS 'Slack file ID from files_upload_v2. NULL''d by cleanup after files.delete.';
COMMENT ON COLUMN requests.row_count IS 'Number of rows returned (or affected, for write queries).';
COMMENT ON COLUMN requests.truncated IS 'TRUE if result was capped at max_rows.';

COMMENT ON TABLE request_notifications IS
$$Tracks every admin DM the bot has posted for a given request, so when one admin acts the bot can chat.update all of them in lockstep — buttons disappear from every admin''s DM, not just the deciding one.$$;

COMMENT ON TABLE audit_log IS
$$Append-only log of state transitions and notable events on requests: submitted, approved, rejected, changes_requested, execution_started, completed, failed. details (JSONB) holds whichever extra context the transition wrote.$$;

COMMENT ON COLUMN audit_log.actor_slack_id IS 'Slack user who took the action. NULL for system-driven steps (executor, cleanup).';
COMMENT ON COLUMN audit_log.details IS 'JSONB context for the action (row_count, error, target_alias, etc.).';

COMMENT ON TABLE access_requests IS
$$A user without team grants can submit one of these to ask for access. Body includes target/database/query they want plus a free-text reason. Admins approve or reject from a DM; the actual team-membership / grant INSERT is done by the admin manually (the bot does not auto-grant). Per (user, target, attempted_query) only one pending row allowed.$$;

COMMENT ON COLUMN access_requests.attempted_query IS 'The SQL the user wanted to run. Optional context for admins; the bot never executes it directly from this table.';
COMMENT ON COLUMN access_requests.status IS 'pending → approved | rejected. CHECK-constrained.';

COMMENT ON TABLE access_request_notifications IS
$$Same lockstep-update mechanism as request_notifications, but for access_requests. Tracks each admin DM so chat.update can replace the button block on every admin''s copy when any admin decides.$$;


-- ============================================================================
-- VIEWS
-- ============================================================================

COMMENT ON VIEW v_team_summary IS
$$Per-team summary: id, name, description, member count, grant count, created_at. Convenience for inspection — used by deploy/team_admin_templates.sql''s "list teams" snippet and by ad-hoc audits.$$;

COMMENT ON VIEW v_user_targets IS
$$For every (Slack user, target) pair the user can reach (via team membership and a team grant), one row with target alias, host, default database, and the per-team allowed_databases array. Answers "what can this user touch?" with a single SELECT.$$;


-- ============================================================================
-- ENUM TYPES
-- ============================================================================

COMMENT ON TYPE request_status IS
$$Lifecycle of a requests row.
  pending → approved   → executing → completed | failed
          → rejected
          → changes_requested
          → cancelled (reserved; not currently emitted by code).$$;
