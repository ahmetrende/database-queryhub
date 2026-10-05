# QueryHub — operations cheatsheet

Load the bot environment first: `set -a; source /etc/queryhub/env; set +a`.
Most commands assume it, and anything that connects to the bot DB needs it.

This page holds all CLI commands, DB configs and admin SQL snippets in one
place, ready to copy and paste. Each section stands alone, so read only what
you need.

> **Not in this document:**
>
> - Backup and restore of the control plane, and what happens if you lose the
>   master key: [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md).
> - Replacing the master key without downtime: [KEY_ROTATION.md](KEY_ROTATION.md).
> - What personal data the system stores, for how long, and how to answer a
>   data-subject request: [COMPLIANCE.md](COMPLIANCE.md).

## Contents

1. [Bot lifecycle](#1-bot-lifecycle)
2. [Encrypted secrets](#2-encrypted-secrets)
3. [Bot config knobs (bot_config table)](#3-bot-config-knobs-bot_config-table)
4. [Migrations](#4-migrations)
5. [Targets — add / disable / rotate](#5-targets--add--disable--rotate)
6. [Admins](#6-admins)
7. [Requesters (allowlist + bypass)](#7-requesters-allowlist--bypass)
8. [Teams + members + grants](#8-teams--members--grants)
9. [Per-user grants (overrides)](#9-per-user-grants-overrides)
10. [Audit / inspection queries](#10-audit--inspection-queries)
11. [Master key + crypto](#11-master-key--crypto)
12. [Maintenance](#12-maintenance)
13. [Ratings & feedback](#13-ratings--feedback)
14. [Multi-statement / SET prelude](#14-multi-statement--set-prelude)
15. [Product metrics (p_metrics_*)](#15-product-metrics-p_metrics_)
16. [Admin scopes (role-based approval)](#16-admin-scopes-role-based-approval)
17. [Keeping real identifiers out of what you share](#17-keeping-real-identifiers-out-of-what-you-share)
18. [Batch submissions (`/sql batch`)](#18-batch-submissions-sql-batch)
19. [Auto-approve grants](#19-auto-approve-grants)
20. [Milestone annotations](#20-milestone-annotations)
21. [Temporary admin grants (vacation / on-call coverage)](#21-temporary-admin-grants-vacation--on-call-coverage)
22. [Excluding test traffic from product metrics](#22-excluding-test-traffic-from-product-metrics)
23. [Publishing the metrics dashboard to S3](#23-publishing-the-metrics-dashboard-to-s3)
24. [Monitoring: /metrics and structured logs](#24-monitoring-metrics-and-structured-logs)
25. [Super-admin elevation on a target](#25-super-admin-elevation-on-a-target)
26. [Read replicas](#26-read-replicas)
27. [Verifying target certificates](#27-verifying-target-certificates)
28. [Splitting the metadata database roles](#28-splitting-the-metadata-database-roles)

---

## 1. Bot lifecycle

```bash
# Status / health
systemctl is-active queryhub
systemctl status queryhub --no-pager | head -15

# Restart (after any code or env change)
sudo systemctl restart queryhub

# Live logs
sudo journalctl -u queryhub -f

# Last 50 lines
sudo journalctl -u queryhub -n 50 --no-pager

# Errors only, last hour
sudo journalctl -u queryhub --since "1 hour ago" --no-pager | grep -iE "ERROR|WARN"
```

**Service file**: `/etc/systemd/system/queryhub.service` →
`WorkingDirectory=<repo-path>`, `EnvironmentFile=/etc/queryhub/env`. The
service runs as the user that owns the repo (the user you chose at install
time). For the placeholder substitution, see `deploy/INSTALL.md`.

**Update flow**: `cd <repo-path> && git pull && sudo systemctl restart queryhub`.

---

## 2. Encrypted secrets

The 3 secrets (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `BOT_DB_PASSWORD`) live
encrypted in `/etc/queryhub/secrets.enc` (Fernet via `master.key`). The
plaintext `/etc/queryhub/env` no longer contains any secrets.

```bash
# Show metadata (which keys are present, mode, mtime — never values)
sudo .venv/bin/python scripts/manage_env_secrets.py list

# First-time setup (prompts for all 3, hidden input)
sudo .venv/bin/python scripts/manage_env_secrets.py init

# Update a single key
sudo .venv/bin/python scripts/manage_env_secrets.py set SLACK_BOT_TOKEN
sudo .venv/bin/python scripts/manage_env_secrets.py set BOT_DB_PASSWORD

# Soft-delete the file (renames to .deleted[_N]; bot needs env-var
# fallback to start after this)
sudo .venv/bin/python scripts/manage_env_secrets.py remove

# Always restart after changes
sudo systemctl restart queryhub
```

File format: line 1 is `SLBOT_SECRETS_v1`, a signature that lets a human
identify the file. Line 2 is the Fernet ciphertext. The permissions must be
`0600`: the loader rejects any group/other bits.

---

## 3. Bot config knobs (bot_config table)

All runtime tunables live here. Edit them with SQL. The bot reads them on each
relevant operation, so a change needs no restart unless noted.

```sql
-- See everything:
SELECT key, value, left(description, 100) AS desc FROM bot_config ORDER BY key;

-- Update a single key:
UPDATE bot_config SET value = '<new>' WHERE key = '<key>';
```

| Key | Default | Restart? | What it does |
|---|---|---|---|
| `bot_display_icon` | `:query_hub:` | no | Emoji that the bot uses as its avatar in chat. Upload it to the workspace as a custom emoji. |
| `bot_display_name` | `QueryHub` | no | Username shown in chat |
| `csv_size_mb` | `10` | no | Max CSV file size. Result streaming aborts at this cap |
| `kill_switch` | `off` | no | Master kill switch. `on` blocks new submissions and scheduler dispatch. Admins can still approve |
| `kill_switch_message` | (banner text) | no | Ephemeral message shown when kill_switch is on |
| `log_level` | `INFO` | yes | Python logging level |
| `max_open_requests_per_user` | `5` | no | Cap on in-flight requests per non-admin Slack user (pending/approved/scheduled/executing) |
| `max_rows` | `1000` | no | Max rows returned in CSV |
| `max_schedule_days` | `7` | no | Max future days for `/sql` scheduling. Set `0` to disable scheduling |
| `min_query_length` | `6` | no | The bot rejects queries shorter than this |
| `pre_flight_explain` | `off` | no | Runs EXPLAIN at modal-submit time to catch typos. RO queries only: the bot skips RW/DDL automatically |
| `query_plan_logging` | `off` | no | When pre_flight_explain AND this key are on, the bot stores the EXPLAIN plan in `requests.explain_plan` (capped at 64KB) |
| `query_timeout_sec` | `300` | no | Per-query `statement_timeout` (5 min) |
| `require_justification` | `false` | no | Requires the justification field for RO queries too. RW/DDL always require it |
| `results_ttl_hours` | `72` | no | How long the bot keeps CSV results, in Slack and locally, before cleanup deletes them. The default is short on purpose: it limits how long sensitive result data stays in Slack |
| `rating_enabled` | `on` | no | Sends a DM with a 1-5 rating prompt after every terminal-state request. The bot suppresses it for 30 days after a user's most recent rating. See section 13 |
| `set_allowed_params` | (~23 params) | no | Comma-separated list of Postgres parameters allowed in a `SET LOCAL` prelude. See section 14 |

Common toggles:

```sql
-- Maintenance window: stop new traffic
UPDATE bot_config SET value = 'on' WHERE key = 'kill_switch';
-- Resume:
UPDATE bot_config SET value = 'off' WHERE key = 'kill_switch';

-- Tighten / loosen the row cap:
UPDATE bot_config SET value = '5000' WHERE key = 'max_rows';

-- Disable scheduling entirely:
UPDATE bot_config SET value = '0' WHERE key = 'max_schedule_days';

-- Boost a power-user (admin always exempt; this raises the cap globally):
UPDATE bot_config SET value = '20' WHERE key = 'max_open_requests_per_user';

# RO-burst nudge (modal banner that offers a short RO auto-approve window):
#   ro_burst_threshold   — min RO requests in the window before the nudge shows (default 3)
#   ro_burst_window_min  — look-back window in minutes for that count       (default 10)
#   ro_window_minutes    — length of the auto-approve window granted on approval (default 60)
UPDATE bot_config SET value = '5'  WHERE key = 'ro_burst_threshold';
UPDATE bot_config SET value = '15' WHERE key = 'ro_burst_window_min';
UPDATE bot_config SET value = '120' WHERE key = 'ro_window_minutes';

# Stale-grant reaper (scripts/reap_stale_grants.py, run daily):
#   grant_reaper_enabled    — master switch (on|off). OFF = dry-run report only.
#   grant_idle_revoke_days  — soft-revoke a user grant idle this many days (default 30)
# Review dry-runs first (.venv/bin/python scripts/reap_stale_grants.py), then enable:
UPDATE bot_config SET value = 'on' WHERE key = 'grant_reaper_enabled';
```

---

## 4. Migrations

Migrations are idempotent SQL files under `migrations/`, numbered in sequence.

```bash
# Apply all (skips already-applied; commits per file)
.venv/bin/python scripts/apply_migrations.py

# What's applied: the runner records each file in the schema_migrations
# ledger (version + checksum), so a re-run applies only what is pending.
.venv/bin/python scripts/apply_migrations.py --dry-run
```

To add a new migration:
1. Create `migrations/NNN_name.sql` with idempotent DDL/DML
2. Run `apply_migrations.py`
3. Commit the file

Files stay idempotent even with the ledger (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF
NOT EXISTS`, `ON CONFLICT DO NOTHING`). Never edit an applied file. The ledger
refuses a changed checksum, so put each change in a new file.

---

## 5. Targets — add / disable / rotate

A target is a Postgres cluster that the bot can query. Each target is a row in
`target_servers`, with Fernet-encrypted credentials per tier (RO is required,
RW/DDL are optional).

### List / inspect

```sql
-- All targets (creds masked):
SELECT id, alias, host, port, default_database, enabled,
       (username   IS NOT NULL) AS has_ro_creds,
       (username_rw  IS NOT NULL) AS has_rw_creds,
       (username_ddl IS NOT NULL) AS has_ddl_creds,
       notes
FROM target_servers
ORDER BY alias;

-- One target's grants (who has access):
SELECT * FROM v_user_targets WHERE target_alias = 'acme-prod-orders';
```

### Encrypt a password for INSERT

```bash
.venv/bin/python scripts/encrypt_secret.py
# Prompts twice (no echo), prints Fernet ciphertext to stdout.
# Paste into the SQL below.
```

### Add a new target (RO only first, RW/DDL later)

```sql
INSERT INTO target_servers
    (alias, host, port, default_database,
     username, password_encrypted, enabled, notes)
VALUES
    ('acme-prod-orders',
     'acme-prod-orders.<aws-id>.<region>.rds.amazonaws.com',
     5432,
     'orders',
     'queryhub_ro',
     '<paste Fernet ciphertext from encrypt_secret.py>',
     TRUE,
     'orders cluster, prod');
```

### Add RW credentials to an existing target

```sql
UPDATE target_servers
   SET username_rw         = 'queryhub_rw',
       password_rw_encrypted = '<paste Fernet ciphertext>'
 WHERE alias = 'acme-prod-orders';
```

### Add DDL credentials (when first DDL request arises)

```sql
UPDATE target_servers
   SET username_ddl         = 'queryhub_ddl',
       password_ddl_encrypted = '<paste Fernet ciphertext>'
 WHERE alias = 'acme-prod-orders';
```

### Rotate a credential

```sql
-- Same as add — just overwrite:
UPDATE target_servers
   SET password_encrypted = '<new Fernet ciphertext>'
 WHERE alias = 'acme-prod-orders';
```

### Enable / disable

```sql
UPDATE target_servers SET enabled = FALSE WHERE alias = 'acme-prod-orders';
UPDATE target_servers SET enabled = TRUE  WHERE alias = 'acme-prod-orders';
```

### Bulk-import from inventory (RDS-side `inventory.v_all_databases`)

```bash
.venv/bin/python scripts/import_targets_from_inventory.py
# Adds new endpoints DISABLED, with the sentinel 'PASSWORD_NOT_SET' password:
# fill real credentials, then enable. A name already in use is settled by
# targets.claim_alias() -- see SCHEMA.md, target_servers.alias.
```

---

## 6. Admins

Admins approve or reject requests. They bypass team grants and the allowlist,
and their queries can run against any enabled target.

```sql
-- List active admins
SELECT slack_user_id, name, email, enabled, added_at
  FROM admins WHERE enabled = TRUE ORDER BY added_at;

-- Add an admin
INSERT INTO admins (slack_user_id, name, added_by)
VALUES ('U01ABCDEFG', 'Person Name', 'system');

-- Disable (reversible)
UPDATE admins SET enabled = FALSE WHERE slack_user_id = 'U01ABCDEFG';

-- Re-enable
UPDATE admins SET enabled = TRUE  WHERE slack_user_id = 'U01ABCDEFG';
```

The bot refreshes `name` and `email` from Slack automatically on every
interaction (profile_sync). You can leave them blank on insert.

---

## 7. Requesters (allowlist + bypass)

This table is the allowlist for `/sql`. Only enabled requesters can submit.
Exception: if the table is empty (zero enabled rows), the bot is OPEN to
everyone in the workspace.

```sql
-- List
SELECT slack_user_id, name, email, enabled, bypass_team_grants
  FROM requesters ORDER BY added_at;

-- Add
INSERT INTO requesters (slack_user_id, name, added_by)
VALUES ('U01ABCDEFG', 'Person Name', 'system');

-- Disable / re-enable
UPDATE requesters SET enabled = FALSE WHERE slack_user_id = 'U01ABCDEFG';
UPDATE requesters SET enabled = TRUE  WHERE slack_user_id = 'U01ABCDEFG';
```

### Bypass team grants

`bypass_team_grants = TRUE` lets the user see every enabled target, with the
same visibility as an admin. The user can also submit any tier (a synthetic
'ddl' grant on everything). The user is still NOT an admin, and cannot approve
or reject.

```sql
-- Grant bypass
UPDATE requesters
   SET bypass_team_grants = TRUE
 WHERE slack_user_id = 'U01ABCDEFG';

-- Remove bypass
UPDATE requesters
   SET bypass_team_grants = FALSE
 WHERE slack_user_id = 'U01ABCDEFG';
```

---

## 8. Teams + members + grants

Teams own targets (through `team_target_grants`). Users belong to teams
(through `team_members`). On a given target, a user's effective grant is the
most permissive of their team grants.

### Inspect

```sql
-- Teams summary
SELECT * FROM v_team_summary ORDER BY name;

-- Members of a team
SELECT t.name AS team, tm.slack_user_id, r.name
  FROM team_members tm
  JOIN teams t      ON t.id = tm.team_id
  LEFT JOIN requesters r ON r.slack_user_id = tm.slack_user_id
 WHERE t.name = 'payments'
 ORDER BY tm.added_at;

-- All grants of a team
SELECT t.name AS team, ts.alias AS target, g.mode,
       g.allowed_databases, g.target_role
  FROM team_target_grants g
  JOIN teams t          ON t.id = g.team_id
  JOIN target_servers ts ON ts.id = g.target_server_id
 WHERE t.name = 'payments';

-- Effective view for a single user
SELECT * FROM v_effective_user_grants
 WHERE slack_user_id = 'U01ABCDEFG';
```

### Create / mutate

```sql
-- New team
INSERT INTO teams (name, description) VALUES ('payments', 'Payments squad');

-- Add a member
INSERT INTO team_members (team_id, slack_user_id)
SELECT id, 'U01ABCDEFG' FROM teams WHERE name = 'payments'
ON CONFLICT DO NOTHING;

-- Remove a member
DELETE FROM team_members
 WHERE team_id = (SELECT id FROM teams WHERE name = 'payments')
   AND slack_user_id = 'U01ABCDEFG';

-- Grant a team RO on a target (entire RDS = NULL allowed_databases)
INSERT INTO team_target_grants
    (team_id, target_server_id, allowed_databases, mode, target_role)
VALUES
    ((SELECT id FROM teams           WHERE name = 'payments'),
     (SELECT id FROM target_servers  WHERE alias = 'acme-prod-orders'),
     NULL, 'ro', NULL)
ON CONFLICT (team_id, target_server_id) DO UPDATE
   SET mode = EXCLUDED.mode,
       allowed_databases = EXCLUDED.allowed_databases;

-- Same but restrict to specific DBs
INSERT INTO team_target_grants
    (team_id, target_server_id, allowed_databases, mode, target_role)
VALUES
    ((SELECT id FROM teams           WHERE name = 'payments'),
     (SELECT id FROM target_servers  WHERE alias = 'acme-prod-orders'),
     ARRAY['orders', 'invoices'], 'rw', NULL);

-- Upgrade a grant to RW
UPDATE team_target_grants
   SET mode = 'rw'
 WHERE team_id          = (SELECT id FROM teams          WHERE name = 'payments')
   AND target_server_id = (SELECT id FROM target_servers WHERE alias = 'acme-prod-orders');

-- Revoke entire grant
DELETE FROM team_target_grants
 WHERE team_id          = (SELECT id FROM teams          WHERE name = 'payments')
   AND target_server_id = (SELECT id FROM target_servers WHERE alias = 'acme-prod-orders');
```

### Postgres-side fence (target_role)

`target_role` lets the executor run `SET LOCAL ROLE <name>` for queries from
this team. This adds an extra defense layer at the cluster level. It is
optional, but recommended for the RW/DDL tier. It is not yet provisioned for
the pilot.

```bash
# Generate runbook for currently-unprovisioned (team, target) pairs:
.venv/bin/python scripts/plan_team_role_provisioning.py > /tmp/runbook.md
```

The runbook lists the psql commands to run on each target cluster. It also
gives the `UPDATE team_target_grants SET target_role = ...` statement that
sets the role on the bot side.

---

## 9. Per-user grants (overrides)

`user_target_grants` overrides team grants for a specific (user, target).
Use it sparingly. Prefer team grants.

```sql
-- See overrides for a user
SELECT ts.alias AS target, ug.mode, ug.allowed_databases
  FROM user_target_grants ug
  JOIN target_servers ts ON ts.id = ug.target_server_id
 WHERE ug.slack_user_id = 'U01ABCDEFG';

-- Grant a single user RW on one target (regardless of their team grant)
INSERT INTO user_target_grants
    (slack_user_id, target_server_id, allowed_databases, mode)
VALUES
    ('U01ABCDEFG',
     (SELECT id FROM target_servers WHERE alias = 'acme-prod-orders'),
     NULL, 'rw')
ON CONFLICT (slack_user_id, target_server_id) DO UPDATE
   SET mode = EXCLUDED.mode,
       allowed_databases = EXCLUDED.allowed_databases;

-- Grant on every enabled target at once (e.g. for a power user)
INSERT INTO user_target_grants
    (slack_user_id, target_server_id, allowed_databases, mode)
SELECT 'U01ABCDEFG', ts.id, NULL, 'rw'
  FROM target_servers ts WHERE ts.enabled = TRUE
ON CONFLICT (slack_user_id, target_server_id) DO UPDATE
   SET mode = EXCLUDED.mode;

-- Drop overrides for a user
DELETE FROM user_target_grants WHERE slack_user_id = 'U01ABCDEFG';
```

**With `access_model_v2` on** (the live model, see
[SCHEMA.md → Access model](SCHEMA.md#access-model)), the resolver does not read
this table. The mirror copies every write above into `access_grant`: one row
per database, marked `mirrored_from = 'user_target_grants'`. The resolver reads
that row, so the statements above still work.

A person's own grant replaces their teams' grants **on the whole server**, not
only on the databases it names. So a personal grant on one database of a
server hides a team grant that the person has on another database there. To
keep the team's grants as well, set `merge_with_team` on the mirrored row:

```sql
-- Keep a person's team grants beside their own grant on one server
-- (dry-run it as a SELECT first, and write an audit_log row in the same
-- transaction)
UPDATE access_grant g SET merge_with_team = TRUE
  FROM principal_identity i
 WHERE i.provider = 'slack' AND i.external_id = 'U01ABCDEFG' AND NOT i.is_deleted
   AND g.principal_id = i.principal_id
   AND g.target_id = (SELECT id FROM target_servers WHERE alias = 'acme-prod-orders')
   AND NOT g.auto_approve AND g.revoked_at IS NULL AND NOT g.is_deleted;
```

The flag stays until the person's `user_target_grants` row changes scope or
tier. Then the mirror replaces the row without the flag. Set the flag again
after such a change. The Effective access screen shows the result: under the
team's grant, it names each member whose own grant applies instead.

---

## 10. Audit / inspection queries

### A — what's currently in flight

```sql
SELECT id, status, requester_slack_id, requester_name,
       (SELECT alias FROM target_servers WHERE id = r.target_server_id) AS target,
       database_name,
       created_at AT TIME ZONE 'Europe/Istanbul' AS submitted_tr,
       scheduled_for AT TIME ZONE 'Europe/Istanbul' AS scheduled_tr
  FROM requests r
 WHERE status IN ('pending', 'approved', 'scheduled', 'executing')
 ORDER BY created_at DESC;
```

### B — recent requests (last 30 days, excluding yourself)

```sql
SELECT
    r.id,
    r.created_at AT TIME ZONE 'Europe/Istanbul' AS submitted_tr,
    r.status,
    r.requester_slack_id,
    coalesce(r.requester_name, rq.name, '(?)')  AS by_name,
    rq.email                                    AS by_email,
    ts.alias                                    AS target,
    r.database_name                             AS db,
    regexp_replace(left(r.query, 120), E'\\s+', ' ', 'g') AS preview,
    r.row_count,
    r.error_message,
    r.decided_by_name,
    EXTRACT(EPOCH FROM (r.decided_at  - r.created_at))::int  AS wait_for_decision_s,
    EXTRACT(EPOCH FROM (r.completed_at - r.executed_at))::int AS exec_duration_s
FROM requests r
LEFT JOIN target_servers ts ON ts.id = r.target_server_id
LEFT JOIN requesters    rq ON rq.slack_user_id = r.requester_slack_id
WHERE r.requester_slack_id <> '<YOUR_SLACK_USER_ID>'   -- replace with your Slack user ID
  AND r.created_at >= NOW() - INTERVAL '30 days'
ORDER BY r.created_at DESC;
```

### C — full detail for a single request

```sql
SELECT *
  FROM requests
 WHERE id = 42;            -- replace with request id
```

### D — usage summary by user

```sql
SELECT
    coalesce(rq.name, r.requester_name, '(?)') AS by_name,
    rq.email,
    r.requester_slack_id,
    count(*)                                                 AS total,
    count(*) FILTER (WHERE r.status = 'completed')           AS completed,
    count(*) FILTER (WHERE r.status = 'failed')              AS failed,
    count(*) FILTER (WHERE r.status = 'rejected')            AS rejected,
    count(*) FILTER (WHERE r.status IN ('pending','approved','scheduled','executing'))
                                                             AS in_flight,
    sum(coalesce(r.row_count, 0))                            AS total_rows,
    max(r.created_at)                                        AS last_request_at
FROM requests r
LEFT JOIN requesters rq ON rq.slack_user_id = r.requester_slack_id
GROUP BY 1, 2, 3
ORDER BY total DESC;
```

### E — every action on a request (audit_log)

```sql
SELECT
    a.created_at AT TIME ZONE 'Europe/Istanbul' AS at_tr,
    a.action,
    a.actor_slack_id,
    a.actor_name,
    a.details
  FROM audit_log a
 WHERE a.request_id = 42                       -- replace
 ORDER BY a.created_at;
```

### F — every audit-log action (last 7 days, excluding yourself)

```sql
SELECT
    a.created_at AT TIME ZONE 'Europe/Istanbul' AS at_tr,
    a.action,
    a.actor_slack_id,
    a.actor_name,
    a.request_id,
    r.requester_name AS request_owner_name,
    ts.alias         AS target
  FROM audit_log a
  LEFT JOIN requests       r  ON r.id = a.request_id
  LEFT JOIN target_servers ts ON ts.id = r.target_server_id
 WHERE a.created_at >= NOW() - INTERVAL '7 days'
   AND (a.actor_slack_id IS NULL OR a.actor_slack_id <> '<YOUR_SLACK_USER_ID>')
 ORDER BY a.created_at DESC;
```

### G — requests with EXPLAIN plans captured

```sql
SELECT id, requester_slack_id,
       jsonb_pretty(explain_plan) AS plan
  FROM requests
 WHERE explain_plan IS NOT NULL
 ORDER BY id DESC LIMIT 10;
```

---

## 11. Master key + crypto

```bash
# Where it lives
ls -la /etc/queryhub/master.key   # mode 600 ubuntu:ubuntu, 44 bytes

# Fingerprint sidecar (optional sanity check)
cat /etc/queryhub/master.key.fingerprint

# First-time generation (one-shot, with backup ritual)
sudo .venv/bin/python scripts/init_master_key.py

# Encrypt a single secret (for INSERT into target_servers.password_*)
.venv/bin/python scripts/encrypt_secret.py
```

The same key:
- Decrypts `target_servers.password_encrypted` (and `_rw_encrypted`,
  `_ddl_encrypted`)
- Decrypts `/etc/queryhub/secrets.enc` (Slack tokens + bot DB password)

**If you rotate this key, you must also re-encrypt every dependent
secret with the new key.** `scripts/rotate_master_key.py` does that. KEY_ROTATION.md describes the procedure. Do not rotate
the key casually.

---

## 12. Maintenance

### CSV results cleanup

```bash
# Manual run (deletes local + Slack uploads older than results_ttl_hours)
.venv/bin/python scripts/cleanup_old_results.py

# Schedule daily via cron, a systemd timer, or your job runner of
# choice. See deploy/INSTALL.md section 12 for a systemd timer
# template.
```

### Disk usage

```bash
du -sh /var/lib/queryhub/results /var/log/queryhub 2>/dev/null
```

### Forced shutdown if systemctl restart hangs

```bash
sudo systemctl stop queryhub           # waits up to 90s
sudo systemctl kill --signal=SIGKILL queryhub
sudo systemctl start queryhub
```

### Quick health snapshot (one query)

```sql
SELECT 'kill_switch'                AS k, value AS v FROM bot_config WHERE key='kill_switch'
UNION ALL SELECT 'pre_flight_explain', value FROM bot_config WHERE key='pre_flight_explain'
UNION ALL SELECT 'in_flight',
    count(*)::text FROM requests WHERE status IN ('pending','approved','scheduled','executing')
UNION ALL SELECT 'last_24h',
    count(*)::text FROM requests WHERE created_at >= NOW() - INTERVAL '24 hours'
UNION ALL SELECT 'targets_enabled',
    count(*)::text FROM target_servers WHERE enabled = TRUE
UNION ALL SELECT 'requesters_enabled',
    count(*)::text FROM requesters WHERE enabled = TRUE;
```

---

## 13. Ratings & feedback

After each terminal-state request (`completed` / `failed` / `rejected` /
`cancelled`), the bot DMs the requester a 1-5 rating prompt. The bot silently
skips a user who rated anything in the last 30 days (cooldown). Low ratings
(1-2) get a contextual "What went wrong?" follow-up button that opens a
feedback modal. High ratings get an "Add feedback" button. Each request takes
one rating: the first click locks it.

Storage: `request_ratings (request_id, slack_user_id, rating 1-5,
feedback_text, rated_at)`. See `migrations/019_request_ratings.sql`.

### Disable / re-enable

```sql
-- Stop showing rating prompts (existing ratings stay)
UPDATE bot_config SET value = 'off' WHERE key = 'rating_enabled';

-- Resume
UPDATE bot_config SET value = 'on'  WHERE key = 'rating_enabled';
```

### Reset a user's cooldown (rarely needed — testing)

```sql
DELETE FROM request_ratings WHERE slack_user_id = 'U01ABCDEFG';
```

### Product KPIs (3 views)

```sql
-- Weekly avg rating + low/high counts + with_feedback count
SELECT * FROM p_metrics_rating_weekly LIMIT 12;

-- Weekly response rate: of all terminal requests, what fraction was rated
SELECT * FROM p_metrics_rating_response_rate LIMIT 12;

-- Drill-down for ratings ≤ 2 (with the request preview, for quality review)
SELECT * FROM p_metrics_rating_low_with_feedback LIMIT 50;
```

### Custom queries

```sql
-- Distribution
SELECT rating, count(*) AS n FROM request_ratings GROUP BY 1 ORDER BY 1;

-- Top recent feedback (any rating)
SELECT rated_at, rating, feedback_text
  FROM request_ratings
 WHERE feedback_text IS NOT NULL
 ORDER BY rated_at DESC LIMIT 20;

-- Per-user rating activity
SELECT slack_user_id, count(*) AS n_ratings,
       round(avg(rating)::numeric, 2) AS avg_rating,
       max(rated_at) AS last_rating
  FROM request_ratings
 GROUP BY 1 ORDER BY n_ratings DESC;
```

---

## 14. Multi-statement / SET prelude

Users can submit multiple `;`-separated statements in one /sql request.
Optional `SET LOCAL <param> = <value>` lines can come before them. Rules:

- All non-SET ("main") statements must be the **same tier** (all RO, or
  all RW, or all DDL). The bot rejects a mix, e.g.
  `SELECT 1; UPDATE t SET x=1 WHERE id=1`, with a "mixed-tier" error.
- The bot rewrites `SET ...` to `SET LOCAL ...` automatically
  (transaction-scoped).
- The bot accepts only parameters in `bot_config.set_allowed_params`. It
  rejects others with a friendly error.
- SET must come BEFORE any main statement. The bot rejects trailing SETs.
- For multiple read result sets, each SELECT writes its own CSV. The bot
  zips them into `req_<id>_results_<ts>.zip` for the user.

### Examples

```sql
-- Tune memory and run a heavy query in one approval
SET work_mem = '256MB';
SELECT user_id, count(*) FROM big_table GROUP BY user_id;

-- Two SELECTs → ZIP with two CSVs
SELECT name FROM teams ORDER BY id;
SELECT alias FROM target_servers WHERE enabled = TRUE;
```

### Edit the SET allowlist

The default has ~23 safe tuning parameters (work_mem, statement_timeout,
enable_*, random_page_cost, ...). To add or delete a parameter:

```sql
-- See current
SELECT value FROM bot_config WHERE key = 'set_allowed_params';

-- Update (comma-separated, no spaces necessary)
UPDATE bot_config
   SET value = 'work_mem,statement_timeout,enable_seqscan,...'
 WHERE key = 'set_allowed_params';
```

The bot reads the list on every submission, so no restart is needed.

### Explicitly DO NOT add to the allowlist

These break the security model:

- `search_path` — schema-redirect attack
- `role`, `session_authorization`, `current_user`, `authorization`
  — privilege escalation
- `client_min_messages` — error suppression
- `log_*` — disables audit logging
- `row_security` — RLS bypass

---

## 15. Product metrics (p_metrics_*)

The bot DB has thirteen read-only views. All carry the `p_metrics_` prefix.
You can tune the cost numbers with the `bot_config.cost_*` rows.

### Cost & savings

| View | What |
|---|---|
| `p_metrics_cost_savings` | Single-row USD summary: DBA time saved (this month + lifetime), replica savings (monthly recurring), grand total |

Tunables (edit to match your org):

```sql
-- Per-request DBA minutes saved vs the "open a ticket" path
UPDATE bot_config SET value = '8'   WHERE key = 'cost_dba_minutes_per_request';
-- DBA fully-loaded hourly cost
UPDATE bot_config SET value = '75'  WHERE key = 'cost_dba_hourly_usd';
-- Read replicas the bot replaces
UPDATE bot_config SET value = '5'   WHERE key = 'cost_avoided_replicas';
-- Per-replica monthly cost
UPDATE bot_config SET value = '200' WHERE key = 'cost_per_replica_monthly_usd';
-- Anything else (bastion, BI seat avoidance, ...)
UPDATE bot_config SET value = '0'   WHERE key = 'cost_other_monthly_usd';

SELECT * FROM p_metrics_cost_savings;
```

### Volume (time buckets)

| View | What |
|---|---|
| `p_metrics_volume_daily`   | Daily request volume + status + DAU (last 90 days) |
| `p_metrics_volume_weekly`  | Weekly + WAU (all-time) |
| `p_metrics_volume_monthly` | Monthly + MAU (all-time) |

### Adoption breakdown

| View | What |
|---|---|
| `p_metrics_team_usage`         | Per-team: active users, total/completed/rejected/failed, avg exec time |
| `p_metrics_top_users`          | Per-user leaderboard with 7d/30d/total counts |
| `p_metrics_scheduled_usage`    | Weekly scheduled-feature adoption % + success/cancel |
| `p_metrics_tier_distribution`  | Weekly ro/rw/ddl mix (from leading keyword) |

### Operational health

| View | What |
|---|---|
| `p_metrics_failure_breakdown` | Weekly outcomes + success rate |
| `p_metrics_admin_workload`    | Per-admin approve/reject/changes volume + last activity |

### User satisfaction

| View | What |
|---|---|
| `p_metrics_rating_weekly`              | Weekly avg rating + low/high counts + with_feedback |
| `p_metrics_rating_response_rate`       | Rated / terminal requests % |
| `p_metrics_rating_low_with_feedback`   | Drilldown for ≤2 ratings + query preview |

### Quick exploration

```sql
-- Top 10 users this week
SELECT name, last_7d FROM p_metrics_top_users ORDER BY last_7d DESC LIMIT 10;

-- Team adoption right now
SELECT * FROM p_metrics_team_usage;

-- This week's scheduled %
SELECT * FROM p_metrics_scheduled_usage LIMIT 4;

-- Cost savings dashboard query
SELECT total_saving_this_month_usd, dba_saving_this_month_usd,
       replica_saving_monthly_usd, req_this_month
  FROM p_metrics_cost_savings;
```

---

## 16. Admin scopes (role-based approval)

Every admin starts as a "super admin", who can approve every request. Three
optional scope columns narrow that authority. Set any of them, all of them,
or none. NULL on a column means "no restriction in that dimension".

| Column | Type | Meaning when NULL | Meaning when set |
|---|---|---|---|
| `max_tier` | text | Approves any tier | `'ro'` / `'rw'` / `'ddl'` — highest tier this admin can approve (hierarchy: ro < rw < ddl) |
| `scope_target_ids` | int[] | Any target | Only requests on these `target_servers.id` values |
| `scope_team_ids` | int[] | Any requester team | Only requests from a requester who is a member of at least one of these teams |

A single function resolves the scope: `admins.can_approve`. The button
guards, the DM-button visibility and the modal-submit guards all use it.
When an out-of-scope admin clicks anyway, the bot rejects them with a DM:
"This request is outside your admin scope (tier / target / team)."

Out-of-scope admins still receive the request DM (audit + transparency).
Their DM has a "view only" footer instead of the Approve / Reject / Request
changes buttons.

### Common patterns

```sql
-- DBA-only DDL: keep this admin able to approve RO+RW everywhere, NOT DDL
UPDATE admins SET max_tier = 'rw' WHERE slack_user_id = 'U01ABCDEFG';

-- Team-lead pattern: this admin approves only their team's requests
UPDATE admins
   SET scope_team_ids = ARRAY[
        (SELECT id FROM teams WHERE name = 'payments')
   ]
 WHERE slack_user_id = 'U01ABCDEFG';

-- Target-owner pattern: admin only approves requests on a few targets
UPDATE admins
   SET scope_target_ids = (
        SELECT array_agg(id) FROM target_servers
         WHERE alias IN ('acme-prod-orders', 'acme-prod-payments')
   )
 WHERE slack_user_id = 'U01ABCDEFG';

-- Combination: team-lead with RW max
UPDATE admins
   SET scope_team_ids = ARRAY[(SELECT id FROM teams WHERE name = 'payments')],
       max_tier       = 'rw'
 WHERE slack_user_id = 'U01ABCDEFG';

-- Reset to super admin (all scopes wildcard)
UPDATE admins
   SET scope_team_ids = NULL, scope_target_ids = NULL, max_tier = NULL
 WHERE slack_user_id = 'U01ABCDEFG';
```

### Inspect

```sql
SELECT slack_user_id, name, enabled, max_tier,
       scope_team_ids, scope_target_ids
  FROM admins ORDER BY enabled DESC, name;

-- Resolve scope-id arrays to readable names
SELECT a.slack_user_id, a.name, a.max_tier,
       (SELECT array_agg(t.name ORDER BY t.name)
          FROM unnest(a.scope_team_ids) AS sid
          JOIN teams t ON t.id = sid)        AS teams,
       (SELECT array_agg(ts.alias ORDER BY ts.alias)
          FROM unnest(a.scope_target_ids) AS sid
          JOIN target_servers ts ON ts.id = sid) AS targets
  FROM admins a
 WHERE a.enabled = TRUE
 ORDER BY a.name;
```

### Watch out for

- **At least one super admin** (all scopes NULL) should exist. If
  every admin has a non-NULL scope, some requests may have no eligible
  approver and stay pending.
- **No-team requesters** do not match a non-NULL `scope_team_ids`. If you
  scope an admin to `team_ids`, that admin cannot approve requests from
  standalone users (users with only `user_target_grants`). Either widen the
  admin's scope, or grant a super admin alongside.
- **Tier changes when a query is edited** through Request-changes.
  The bot re-classifies the query on resubmit, and verifies the admin scope
  again, against the new tier.

---

## 17. Keeping real identifiers out of what you share

Almost everything QueryHub puts on a screen names something real: a
connection alias, a database, a hostname in an error message, the person
who asked. In normal use, that is the point. It becomes a problem as soon as
any of it leaves the deployment. For example: a screenshot in a ticket, a log
excerpt in a chat, a config snippet in a bug report upstream.

It leaks most easily in four places:

- **The connection list and the audit log.** Both are dense with aliases by
  design. Crop or redact them before you paste.
- **Error text.** `errors.py` scrubs libpq messages before a user sees
  them, but the unscrubbed original is in the service log.
- **Result files.** A CSV or XLSX under `QH_RESULTS_DIR` is real data
  until the retention job deletes it (`results_ttl_hours`).
- **A fork of this repository.** If you commit your own `bot_config`
  rows, migrations or fixtures, the aliases and user ids go with them.

If you maintain a fork, this practice is worth copying. Let the pre-commit
scan build its denylist *from the metadata database*, not from a hand-kept
list. The denylist then contains every alias, host, team name and user id
that the database currently holds. So the list cannot go stale as the set of
people and targets changes. Static patterns alone (token shapes, private
keys, RFC1918 addresses) will not catch the thing most likely to leak, which
is a name.


## 18. Batch submissions (`/sql batch`)

A user can submit up to N queries in one approval round. Each item becomes
its own `requests` row, linked by `bundle_id`. The per-item Approve / Reject /
Changes buttons re-use the existing handlers. The "Approve all remaining" /
"Reject all remaining" buttons decide all of one admin's pending items in one
click. When the whole bundle is decided and executed, one summary DM arrives,
with the CSV of every completed item attached.

Every admin gets the batch DM. A scoped approver, such as a pod captain,
gets it too when they can approve every item on its own. A captain's role
reaches RO on their own pod's servers. So for a captain, that means an all-RO
batch from their pod: the same rule that brings them a single RO request. The
bulk buttons admit the same people, and act only on the items in the
presser's scope. A batch with one item outside a captain's scope, such as a
write or another pod's server, stays with the admins.

### Feature flag

```sql
-- Turn on (off by default — modal toggle + sub-command hidden when off).
UPDATE bot_config SET value = 'on' WHERE key = 'batch_enabled';

-- Cap the max items per bundle.
UPDATE bot_config SET value = '5' WHERE key = 'batch_max_items';
```

### How users access it

- `/sql` → the modal shows a *Single ↔ Batch* radio toggle at the top
  when `batch_enabled = 'on'`. A switch keeps whatever the user already
  typed. The single query becomes batch item #1, and batch item #1 becomes
  the single query. A switch from batch to single warns when it
  drops items #2+.
- `/sql batch` → opens the modal directly in batch mode (fast path
  for power users).

### Inspect bundles

```sql
-- All bundles + per-item status mix.
SELECT b.id, b.status, b.requester_slack_id,
       b.created_at, b.scheduled_for,
       count(*)                                   AS items,
       count(*) FILTER (WHERE r.status = 'completed') AS done,
       count(*) FILTER (WHERE r.status = 'failed')    AS failed,
       count(*) FILTER (WHERE r.status = 'rejected')  AS rejected
  FROM request_bundles b
  LEFT JOIN requests   r ON r.bundle_id = b.id
 GROUP BY b.id
 ORDER BY b.created_at DESC
 LIMIT 25;

-- Drill into one bundle.
SELECT r.position, r.status, ts.alias, r.database_name,
       left(r.query, 80) AS q, r.row_count, r.error_message
  FROM requests r JOIN target_servers ts ON ts.id = r.target_server_id
 WHERE r.bundle_id = :bundle_id
 ORDER BY r.position;
```

### Bundle status trigger

A change to `requests.status` fires an AFTER UPDATE trigger
(`trg_recompute_bundle_status`) that recomputes `request_bundles.status`.
The trigger uses `pg_advisory_xact_lock(bundle_id)` to serialise concurrent
recomputes. The rule set:

| Item mix | Bundle status |
|---|---|
| any pending / approved / scheduled / executing / awaiting_dba_manual / changes_requested | `pending` |
| all `cancelled` | `cancelled` |
| at least one `completed` AND at least one terminal-negative | `partial` |
| else (all completed / all rejected / all failed) | `decided` |

### Summary DM idempotency

`request_bundles.requester_summary_message_ts` records the Slack ts of
the requester's bundle-summary DM. The first time the bundle reaches a
terminal state, the bot posts the DM and saves the ts. Later state changes
(e.g. a manually-completed DDL item closed hours later) `chat.update`
the same DM instead of posting a new one.

---

## 19. Auto-approve grants

An auto-approve grant is a per-user, time-bounded, tier-scoped exemption from
admin approval. The bot evaluates grants at submit time AND at scheduled run
time (if scheduled_for is set). If a grant is active now but expires before
the run, the request goes to admin approval instead, with a user-facing
warning.

### Schema cheat sheet

```sql
\d auto_approve_grants
\d v_active_auto_approve
```

`max_tier` is `ro` / `rw` / `ddl`. RO covers RO only. RW covers RO+RW.
DDL covers everything. Queries above the grant's tier use the normal admin
flow.

### With a grant, and only where they can query

A waiver skips review, but it grants no access. So the web panel and
`/sql grant` tie the two together:

- **Grant + auto-approve in one step.** Two boxes write the waiver in the
  grant's own transaction. One is the web grant form's Auto-approve box
  (`POST /admin/grants` with `autoApprove: true`, `autoApproveTier` `ro` by
  default). The other is the "Auto-approve read-only queries" box on
  `/sql grant`. The waiver gets one row per granted database (NULL for all of
  them), with the same target, the same expiry and the reason
  `auto-approve with the grant: <reason>`.

  A person's row goes into `auto_approve_grants`, and the migration-109 mirror
  projects it. A team's row is an `access_grant` row (`auto_approve = TRUE`,
  `team_id` set, `mirrored_from` NULL). QueryHub refuses a tier above the
  grant's, or DDL, before it writes anything.
- **Not written twice.** If the subject already holds an equal or broader
  waiver, the new one is redundant, so QueryHub skips it. Equal or broader
  means any target or the same one, any database or the same one, and a tier
  at least as high. The held waiver must also have started, and it must end
  no sooner. The response's `autoApprove.skipped`, the web toast and the Slack
  summary name the covering row. A team's waiver does not count for a person:
  their own grant on the server displaces the team's rows there.
- **Only where they can query.** `POST /admin/auto-grants` and
  `/admin/auto-grants/bulk` read the subject's access from the
  effective-access resolvers. They refuse a target or database that the
  subject cannot reach, and a tier above the one they hold there. A refusal
  returns `409` and writes nothing. QueryHub does not verify a fleet-wide row
  (no target): it already means "every server they can reach". It does not
  verify rows written by hand in SQL either, so open the person's Effective
  access screen first.

### Grant patterns

```sql
-- 2-week RO for a teammate during reporting season.
INSERT INTO auto_approve_grants
    (slack_user_id, max_tier, expires_at, reason, granted_by)
VALUES
    ('U0XXXXXXXXX', 'ro', NOW() + INTERVAL '14 days',
     'Q4 reporting auto-pull', :your_slack_id);

-- TR-local cutoff: Friday 18:00 Europe/Istanbul.
INSERT INTO auto_approve_grants
    (slack_user_id, max_tier, expires_at, reason, granted_by)
VALUES
    ('U0XXXXXXXXX', 'ro',
     '2026-05-22 18:00:00+03'::timestamptz,
     'Pilot operator', :your_slack_id);

-- Open-ended RW for a trusted on-call engineer.
INSERT INTO auto_approve_grants
    (slack_user_id, max_tier, expires_at, reason, granted_by)
VALUES
    ('U0XXXXXXXXX', 'rw', NULL,
     'Trusted operator', :your_slack_id);

-- One-day DDL window for a planned schema migration.
INSERT INTO auto_approve_grants
    (slack_user_id, max_tier, starts_at, expires_at, reason, granted_by)
VALUES
    ('U0XXXXXXXXX', 'ddl',
     '2026-06-01 08:00:00+03'::timestamptz,
     '2026-06-01 18:00:00+03'::timestamptz,
     'Schema migration window', :your_slack_id);
```

### Inspect

```sql
-- Currently active grants.
SELECT * FROM v_active_auto_approve ORDER BY max_tier DESC, expires_at NULLS LAST;

-- Everything ever issued for one user.
SELECT id, max_tier, starts_at, expires_at, reason, granted_by, granted_at
  FROM auto_approve_grants
 WHERE slack_user_id = 'U0XXXXXXXXX'
 ORDER BY granted_at DESC;

-- All auto-approved requests in the last 7 days (who, when, what).
SELECT r.id, r.requester_slack_id, ts.alias, r.database_name,
       r.decided_by_name, r.status, r.created_at
  FROM requests r JOIN target_servers ts ON ts.id = r.target_server_id
 WHERE r.decided_by_slack_id = 'AUTO'
   AND r.created_at > NOW() - INTERVAL '7 days'
 ORDER BY r.created_at DESC;
```

### Revoke

```sql
-- Hard delete.
DELETE FROM auto_approve_grants WHERE id = :grant_id;

-- Or expire in place (preserves history).
UPDATE auto_approve_grants SET expires_at = NOW() WHERE id = :grant_id;
```

### Behavioural notes

- A modal banner appears at the top of the `/sql` modal whenever the user
  has any active grant. The banner reads ":zap: Auto-approve active — up to
  RO, until …".
- On INSERT, an auto-approved request gets `status=approved` (or
  `'scheduled'`), `decided_by_slack_id='AUTO'`, and a
  `decided_by_name` like `auto-approved (grant #N, max_tier=ro, until ...)`.
- Every active admin gets a short FYI DM per auto-approved request: a header
  and the inline query (truncated at 500 chars). The bundle FYI puts all
  auto-approved items in one DM per admin.
- A higher-tier query (e.g. the user has an RO grant and submits RW) goes to
  the standard pending → admin approval flow instead. There is no silent
  privilege escalation.
- Scheduling guardrail: if `scheduled_for` is AFTER `expires_at`, the submit
  handler sends the request to admin approval instead. It warns the user in
  the confirmation DM.

---

## 20. Milestone annotations

`metric_annotations` is a free-form table that marks notable moments on the
product-metrics timeline (go-live, access cutover, incident, config change).
The `p_metrics_usage_daily` view joins these by day, so a dashboard can label
its bars.

```sql
-- Add an annotation (TR-local time).
INSERT INTO metric_annotations (occurred_at, label, description)
VALUES ('2026-06-01 10:00+03', 'Rollout v2',
        'Wave 2 teams onboarded.');

-- List in chronological order.
SELECT id, occurred_at AT TIME ZONE 'Europe/Istanbul' AS local_ts,
       label, description
  FROM metric_annotations
 ORDER BY occurred_at;

-- Days with usage AND annotations.
SELECT day, submitted, completed, active_users, annotations
  FROM p_metrics_usage_daily
 WHERE annotations IS NOT NULL
 ORDER BY day DESC;
```

A UNIQUE(`occurred_at, label`) constraint makes the migration seed safe to re-run.



## 21. Temporary admin grants (vacation / on-call coverage)

A temporary admin grant is a time-bounded admin role. A **super-admin** (a
permanent admin with ALL scope columns NULL) can deputise someone for a
defined window. The deputy automatically loses admin status the moment
`expires_at` passes.

### Who is a super-admin?

```sql
-- The set of users allowed to issue temp grants.
SELECT slack_user_id, name
  FROM admins
 WHERE enabled = TRUE
   AND max_tier         IS NULL
   AND scope_team_ids   IS NULL
   AND scope_target_ids IS NULL;
```

### Grant a temp admin

```sql
-- 2-week full coverage during a vacation.
INSERT INTO temp_admin_grants
    (slack_user_id, expires_at, reason, granted_by)
VALUES ('U0XXXXXXXXX',
        NOW() + INTERVAL '14 days',
        'Vacation coverage for @alex',
        :your_slack_id);

-- RO-only deputy for a specific team during off-hours.
INSERT INTO temp_admin_grants
    (slack_user_id, max_tier, scope_team_ids, expires_at,
     reason, granted_by)
VALUES ('U0YYYYYYYYY',
        'ro', ARRAY[2]::int[],
        NOW() + INTERVAL '3 days',
        'Weekend on-call (payments RO only)',
        :your_slack_id);

-- Scheduled future window (e.g., during a planned migration).
INSERT INTO temp_admin_grants
    (slack_user_id, max_tier, starts_at, expires_at,
     reason, granted_by)
VALUES ('U0ZZZZZZZZZ',
        'ddl',
        '2026-06-01 08:00:00+03'::timestamptz,
        '2026-06-01 20:00:00+03'::timestamptz,
        'Schema migration window admin',
        :your_slack_id);
```

The bot keeps the permanent `admins` table immutable. Temp grants go into
`temp_admin_grants`, and `is_admin` / `can_approve` / `list_active` consult
both tables.

> **Not in force while `access_model_v2` is on.** Under the new model,
> those three answers come from `access.py`. It reads `role_assignment`
> and does not know this table. The migration-109 mirror does not project
> it either, so a row written here decides nothing. This gap caught nobody,
> because the table never held a row.
>
> Do not use this recipe to arrange on-call cover until one of two changes
> is made. Either the temp grant is mirrored into `role_assignment`, or a
> time-bounded `admin` role row replaces it. `role_assignment.valid_until`
> already supports such a row. In the meantime, write the role row directly.

### Inspect

```sql
-- Currently active.
SELECT * FROM v_active_temp_admins
 ORDER BY max_tier DESC NULLS FIRST, expires_at NULLS LAST;

-- Full history (including expired and revoked).
SELECT id, slack_user_id, max_tier, starts_at, expires_at,
       reason, granted_by, granted_at, revoked_at
  FROM temp_admin_grants
 ORDER BY granted_at DESC
 LIMIT 30;

-- One user's complete temp admin trail.
SELECT id, max_tier, scope_team_ids, scope_target_ids,
       starts_at, expires_at, reason, granted_by, revoked_at
  FROM temp_admin_grants
 WHERE slack_user_id = 'U0XXXXXXXXX'
 ORDER BY granted_at DESC;
```

### Revoke early

```sql
-- Soft-revoke: keeps the row for audit, but the deputy loses
-- admin status immediately on the next is_admin() check.
UPDATE temp_admin_grants
   SET revoked_at = NOW()
 WHERE id = :grant_id
   AND revoked_at IS NULL;
```

### Python helpers

If you prefer Python over raw SQL:

```python
from queryhub import admins
from datetime import datetime, timedelta, timezone

# Returns the new grant_id; raises admins.NotASuperAdmin if the
# granter isn't a super-admin.
gid = admins.grant_temp_admin(
    granted_by='U0XXXXXXXXX',                 # the super-admin
    slack_user_id='U0YYYYYYYYY',              # the deputy
    expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    reason='Vacation coverage',
    max_tier='ro',                             # optional; default = any
    scope_team_ids=[2],                        # optional; default = any
)

admins.revoke_temp_admin(granted_by='U0XXXXXXXXX', grant_id=gid)

# Read paths
admins.is_admin('U0YYYYYYYYY')   # True during the active window
admins.list_active()             # source column = 'permanent' | 'temp'
admins.list_temp_grants('U0YYYYYYYYY')
```

### Visibility

- `/sql whoami` shows active temp grants on the deputy's own profile,
  with the expiry per grant.
- Temp admins receive every per-request admin DM (they're in
  `admins.list_active()`).
- Approval / reject buttons respect their scope (`can_approve()`
  evaluates both permanent and temp rows).
- The admin DM "Approved by @user @ `2026-05-20 17:38 UTC`" does not
  distinguish permanent and temp admins. The Slack id is enough for audit,
  and the `temp_admin_grants` table holds the per-grant context.


## 22. Excluding test traffic from product metrics

The `report_excluded_users` table excludes operator self-tests from every
`p_metrics_*` view (volume, top users, admin workload, ratings, cost savings).
Without it, the self-tests would pollute those views. The table is an
allowlist-of-exclusions. Three thin wrapper views consult it
(`requests_reportable`, `audit_log_reportable`, `request_ratings_reportable`),
and every metric view reads from those instead of the raw base tables.

The bot's runtime paths (kill-switch, allowlist, team grants, admin scope,
audit_log) all keep reading the raw tables. The exclusion is metrics-only.

### Filter semantics

| View family | Filter applied |
|---|---|
| `requests_reportable` | drops rows where `requester_slack_id` is in `report_excluded_users` |
| `audit_log_reportable` | drops rows whose linked `request_id` is itself dropped from `requests_reportable`. Actor identity alone never excludes a row. An excluded user's approvals of OTHER people's real requests stay visible in admin reports, so the reports keep that user's actual DBA workload. |
| `request_ratings_reportable` | drops ratings whose rater is excluded OR whose underlying request was dropped from `requests_reportable` |

So an excluded user's **own** requests (and any audit actions on them)
vanish from the reports. Actions that the excluded user took on OTHER
people's real requests stay visible. Admin reports show the user's actual
DBA workload, minus the self-test noise.

### Add / remove a user

```sql
-- Exclude
INSERT INTO report_excluded_users (slack_user_id, reason, added_by)
VALUES ('U0XXXXXXXXX', 'On-call rotation test scripts', :your_slack_id)
ON CONFLICT (slack_user_id) DO NOTHING;

-- Re-include
DELETE FROM report_excluded_users WHERE slack_user_id = 'U0XXXXXXXXX';

-- Who's currently excluded
SELECT slack_user_id, reason, added_by, added_at
  FROM report_excluded_users ORDER BY added_at;
```

A change takes effect on the next view read. It needs no restart, no
re-aggregation and no migration.



## 23. Publishing the metrics dashboard to S3

A job on a systemd timer regenerates the dashboard HTML
(`metrics_dashboard.html`) and uploads it to an S3 bucket. Browsers open a
fronted URL (private ALB / CloudFront with auth) → S3 → fresh-ish HTML. The
refresh interval is the timer cadence (default hourly).

### One-time setup

**What has to exist before the upload works:**

1. Private S3 bucket, e.g. `<company>-internal-dba-dashboards`.
   Block-Public-Access ON, with no public listing.
2. **IAM role** attached to the bot's EC2 instance (preferred) with
   exactly this policy on the dashboard prefix:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["s3:PutObject", "s3:PutObjectAcl"],
       "Resource": "arn:aws:s3:::<bucket>/dba-metrics/*"
     }]
   }
   ```

   (Alternative: an IAM user with an access key written to the bot
   user's `~/.aws/credentials`. Prefer the role approach, because it
   leaves no key to rotate.)
3. Fronting, with one of these two:
   - **CloudFront distribution** with an Origin Access Control (OAC)
     that reads the bucket privately.
   - **Private ALB** with an S3 VPC endpoint.
4. **TLS cert** on the front (ACM).
5. **Internal DNS** record (e.g.
   `dba-metrics.<company-internal>.com`).
6. **Authentication**: the minimum is Cognito (IdP-backed) at the
   CloudFront / ALB layer. Anonymous internal access is also
   acceptable on a tight VPN, but it adds a rotation-of-trust step.

### Bot-side wiring

Once the bucket exists, the operator's side is three files (already
in the repo):

```bash
# 1. Bucket name + key (key is optional; default is dba-metrics/index.html).
sudo tee /etc/queryhub/dashboard.env >/dev/null <<EOC
METRICS_DASHBOARD_BUCKET=<the-bucket-name>
METRICS_DASHBOARD_KEY=dba-metrics/index.html
AWS_REGION=eu-central-1
EOC
sudo chown __USER__:__USER__ /etc/queryhub/dashboard.env
sudo chmod 600 /etc/queryhub/dashboard.env

# 2. Drop the systemd unit + timer in place with placeholders
#    substituted (same pattern as queryhub.service in INSTALL.md).
sudo cp deploy/dba-metrics-publish.service /etc/systemd/system/
sudo cp deploy/dba-metrics-publish.timer   /etc/systemd/system/
sudo sed -i "s|__USER__|$BOT_USER|g; s|__INSTALL_PATH__|$REPO_DIR|g" \
     /etc/systemd/system/dba-metrics-publish.{service,timer}

# 3. Enable + start.
sudo systemctl daemon-reload
sudo systemctl enable --now dba-metrics-publish.timer

# Trigger a one-off run to confirm permissions + connectivity.
sudo systemctl start dba-metrics-publish.service
sudo journalctl -u dba-metrics-publish.service -n 30 --no-pager
```

### Operations

```bash
# Next-scheduled timer fire + last-run timestamp.
systemctl list-timers dba-metrics-publish.timer --no-pager

# Recent publish logs (last 5 runs).
sudo journalctl -u dba-metrics-publish.service -n 100 --no-pager

# Force a publish right now (after a config change or an annotation insert).
sudo systemctl start dba-metrics-publish.service

# Disable until DevOps fixes upstream.
sudo systemctl disable --now dba-metrics-publish.timer
```

### Change the cadence

Edit `/etc/systemd/system/dba-metrics-publish.timer`'s `OnCalendar=`
line:

| Cadence | Value |
|---|---|
| Default | `hourly` |
| Every 15 minutes | `*:0/15` |
| Every 5 minutes | `*:0/5` |
| Twice a day (09:00, 17:00 server time) | `OnCalendar=*-*-* 09,17:00:00` |

Then `sudo systemctl daemon-reload && sudo systemctl restart dba-metrics-publish.timer`.

### Cost / footprint sanity

- HTML size: ~50 KB. 24 uploads/day × 50 KB ≈ 1.2 MB/day → bucket
  storage ≈ negligible.
- `Cache-Control: max-age=300, must-revalidate`: readers see a fresh
  copy within 5 minutes of the next upload, even through CloudFront.



---

## 24. Monitoring: `/metrics` and structured logs

An operator needs two things that did not exist before: an endpoint to scrape,
and log lines that a parser can read without a regex.

### `GET /metrics`

Prometheus text format, **off by default**. A self-hosted tool should not start
to publish its queue depth, fleet size and user counts because somebody
upgraded. So enabling it is a decision:

```sql
UPDATE bot_config SET value = 'on' WHERE key = 'web_metrics_enabled';
```

Runtime-effective: no restart. While off, the route answers **404**, not 403.
A 403 would reveal that the endpoint is there.

For a scraper, set a token as well. Prometheus can send a bearer header, but
it cannot hold a session cookie:

```sql
UPDATE bot_config SET value = 'PASTE_TOKEN' WHERE key = 'web_metrics_token';
```

Generate it like any other credential:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

With the token empty, the endpoint requires an **admin session** instead. So
enabling the key alone does not publish it. The endpoint compares the token in
constant time.

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://queryhub.internal/metrics
```

Scrape config:

```yaml
scrape_configs:
  - job_name: queryhub
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials: PASTE_TOKEN
    static_configs:
      - targets: ['queryhub.internal:8080']
```

**The values come from SQL at scrape time, not from in-process counters.** That
is deliberate. Counters in memory reset on restart. Also, the two processes
(`queryhub` and `queryhub-web`) each see only part of the traffic, so neither
would see all of it. Counters here are cumulative over all history, which is
what `rate()` expects.

### What is worth alerting on

| Metric | Why |
| --- | --- |
| `queryhub_oldest_request_age_seconds{status="pending"}` | **The one that matters.** Depth alone cannot tell "three arrived this second" from "one has waited since yesterday". A developer experiences this wait as the tool being broken. |
| `queryhub_requests_in_state{status="executing"}` | Stuck executions. The value should return to 0. A value that never falls means that a lease stays held. |
| `queryhub_kill_switch_active` | 1 means the kill switch stops all new query traffic. It is easy to leave enabled after an incident. |
| `queryhub_auth_event_outbox_depth` | Monotonic growth means that the auth-event poller is not running. QueryHub records grant changes but never announces them. This is a real failure mode. The poller once lived only in the Slack process, so a vanilla install grew this table forever, and nothing said so. |
| `queryhub_scrape_errors` | Non-zero means that some collector failed and the numbers are partial. When every query breaks, the dashboard shows only zeros. That looks exactly like a healthy idle system. |
| `rate(queryhub_requests_total{status="failed"}[15m])` | Execution failures. |

The endpoint exposes durations as `_sum`/`_count` pairs, so
`rate(queryhub_execution_seconds_sum[1h]) / rate(queryhub_execution_seconds_count[1h])`
gives the average over whatever window you pick. There are no quantiles. Real
quantiles need histogram buckets kept in process memory. As explained above,
there is no process memory to keep them in.

One failing collector does not fail the scrape. A partial payload is better
than a 500 at the exact moment something is already wrong.

### Structured logs

`LOG_FORMAT=json` in the service environment switches both processes to one JSON
object per line:

```json
{"timestamp":"2026-07-25T15:26:01+00:00","level":"INFO","logger":"queryhub.executor","message":"executing request 4242 on target demo-primary","request_id":4242,"tier":"ro"}
```

`timestamp` is RFC 3339 in **UTC**, regardless of `web_display_timezone`.
Correlating two hosts across a DST boundary is the kind of problem that costs
an hour at 3am. Tracebacks stay on one line as an `exception` field, and that
is the main reason to switch. In text format, a traceback arrives at the log
backend as N unrelated lines. The line that names the exception is not the line
with the context.

`text` remains the default, because a human who tails `journalctl` is the
common case. JSON is worse for that. Each process reads this setting and
`LOG_LEVEL` once, at process start, so a change needs a restart:

```bash
sudo systemctl restart queryhub queryhub-web
```

---

## 25. Super-admin elevation on a target

A super-admin runs without the tier and safety limits that an ordinary
requester has. The point is not to give them power they lack: a DBA already
has it through their own tooling. The point is that the audit trail *records
the work* they were going to do anyway. The steps below provision the one role
that makes this possible on a cluster.

The model is in `docs/SCHEMA.md` → "Super-admin elevation". This section
is the runbook.

### What a super-admin can do that others cannot

QueryHub refuses each of these for everyone else, as before:
- **End or cancel a session.** `SELECT pg_terminate_backend(pid)` or
  `pg_cancel_backend(pid)`, alone or over `pg_stat_activity`. QueryHub asks
  first. Then it runs the statement at the ddl tier, as the elevated role,
  because the read-only login cannot signal another role's backend. On a
  cluster without the role (no `super_ddl_role`), the DDL login runs it, and
  the server may refuse.
- **Run a mixed script.** A SELECT, an UPDATE and a SELECT go as one request,
  at the script's highest tier, in one transaction.
- **Set `search_path`.** `SET search_path = app, public` before the query.
  QueryHub limits the value to a list of schema names, and keeps it to the one
  request (SET LOCAL).

QueryHub still refuses these for a super-admin too: logging-setting changes,
file reads, `dblink`, `pg_reload_conf`, and replication-slot changes. A
dropped slot breaks the CDC streams that migrations run on.

### Once per cluster

Run as an operator login that holds the platform's admin role
(`rds_superuser` on RDS, `root` on Huawei):

```sql
DO $$
BEGIN
  IF current_setting('server_version_num')::int < 160000 THEN
    RAISE EXCEPTION 'pre-16 server (%): WITH INHERIT FALSE is unavailable',
                    current_setting('server_version');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'queryhub_superadmin') THEN
    CREATE ROLE queryhub_superadmin NOLOGIN CREATEROLE CREATEDB;
  END IF;
  EXECUTE 'GRANT ' || (SELECT rolname FROM pg_roles
                        WHERE rolname IN ('rds_superuser','root')
                        ORDER BY 1 LIMIT 1)
                   || ' TO queryhub_superadmin';
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'queryhub_ddl') THEN
    EXECUTE 'GRANT queryhub_superadmin TO queryhub_ddl '
            'WITH INHERIT FALSE, SET TRUE';
  END IF;
END $$;
```

The version guard is not decoration. `WITH INHERIT FALSE, SET TRUE` needs
PostgreSQL 16+. Without the guard, an older server would create the role,
fail on the grant, and leave the login **inheriting** admin rights in every
session. That is the exact property the design exists to prevent. The safe
outcome is that the whole block fails.

The block picks the admin role from the catalog instead of naming it. So one
statement works on both clouds.

### Then, in the bot DB

```sql
UPDATE target_servers
   SET super_ddl_role = 'queryhub_superadmin'
 WHERE alias = '<target>';
```

Runtime-effective: the executor reads it per request, so no restart is needed.

### Verify from the catalog, not from the absence of errors

```sql
SELECT CASE WHEN pg_has_role('queryhub_superadmin','rds_superuser','USAGE')
             AND EXISTS (SELECT 1 FROM pg_auth_members m
                           JOIN pg_roles r ON r.oid = m.roleid
                           JOIN pg_roles u ON u.oid = m.member
                          WHERE r.rolname = 'queryhub_superadmin'
                            AND u.rolname = 'queryhub_ddl'
                            AND m.inherit_option = false
                            AND m.set_option    = true)
            THEN 'VERIFIED' ELSE 'INCOMPLETE' END;
```

Then prove the property end to end, connected as the DDL user:

```sql
SELECT pg_has_role(current_user,'rds_superuser','USAGE');  -- must be false
BEGIN;
  SET LOCAL ROLE queryhub_superadmin;
  SELECT pg_has_role(current_user,'rds_superuser','USAGE'); -- true, inside only
COMMIT;
SELECT current_user;                                        -- back to the login
```

### What to exclude, and why

- **The control-plane cluster.** `audit_log` lives there. An elevated
  session that can write it could delete its own trail. This exclusion is
  not negotiable.
- **Non-PostgreSQL targets.** The role model is Postgres-specific.
- **Read replicas need nothing.** Roles are cluster-global, so a replica
  shows VERIFIED as soon as its primary is done. Do not write to it.

### Traps measured on the real fleet

- **`target_servers.username_ddl` and the cluster's actual roles disagree
  in both directions.** A stored credential can name a role that nobody
  created, and a role can exist with no stored credential. Neither is
  evidence of the other, so verify the roles on the cluster.
- **A target with zero request history hides this indefinitely.** Grants
  gate visibility, so a target that nobody holds a grant on gets no requests.
  When you audit, start from the targets with no requests.
- **Fernet ciphertext is not deterministic.** Two targets that show
  different `password_ddl_encrypted` blobs may hold the identical
  password. Do not infer a mismatch from the ciphertext.
- **`pg_authid` is not readable on RDS even as `rds_superuser`.** So you
  cannot copy a SCRAM verifier between clusters to clone a login without
  knowing its plaintext.

---

## 26. Read replicas

A read-only request on a target that has a read replica runs on the replica
when the replica is healthy, and on the primary otherwise. Nobody picks a
replica. Every list shows the primary's one name, and the request, its grant
and its history stay on the primary. The one exception is a super-admin, who
can choose the node for a single query (see "Choosing where one query runs"
below). Code: `src/queryhub/replicas.py`.

**How a replica is known.** `target_servers.replica_of` points a replica row at
its primary. The hourly inventory import sets it from `v_server.replica_source`
(step 1c). The import clears it when the inventory stops calling that host a
replica. A target that the inventory does not list keeps a link set by hand.

**What decides where a request runs**, in order:

1. `bot_config.replica_routing = 'on'`. This is the kill switch. The default is `off`.
2. The target is PostgreSQL, and the request runs at the RO tier.
3. The SQL reads nothing that describes the server itself (`pg_stat_*`,
   `pg_locks`, WAL positions, `txid_*` ...). On a replica, those answer about
   the replica.
4. The requester ran no RW/DDL on this target in the last
   `replica_read_your_writes_minutes` (5). So a SELECT that verifies their own
   UPDATE sees that UPDATE.
5. The replica row is **enabled**, and healthy: still in recovery, and at most
   `replica_max_lag_seconds` (10) behind. QueryHub measures lag against the
   primary's current WAL position, then the replica's replay position. Each
   process trusts one health probe for `replica_health_ttl_seconds` (15).

A replica needs no credential of its own. A physical replica has the primary's
roles and passwords, so QueryHub uses the primary's RO login. An admin can
therefore enable a replica row that has only the placeholder password.

**Putting one in or out of rotation.** Enable or disable the replica row, in
the admin Connections screen or with SQL. The importer never enables anything.

```sql
-- Which replicas exist, and which are in rotation
SELECT r.id, r.alias, r.enabled, p.alias AS primary_alias
  FROM target_servers r JOIN target_servers p ON p.id = r.replica_of;
```

**If the replica fails the query.** When the replica is unreachable, the query
runs again on the primary, once. The same happens when the replica cancels the
statement to keep replaying ("conflict with recovery", measured
`max_standby_streaming_delay = 30s` on the fleet). The audit log records a
`replica_fallback` row. QueryHub does not re-run a timeout or a user's cancel.

**Where a request ran.** `requests.executed_target_id` names the replica. The
`execution_started` audit row carries `replica` and `replica_lag_s`. Or it
carries `replica_skipped`, with the reason why QueryHub skipped the replica. The
requester sees "Ran on a read replica ..." in the Slack result and in the web
Messages tab. Neither shows the replica's name.

```sql
-- Read requests served by a replica, last day
SELECT r.id, t.alias AS target, x.alias AS ran_on, r.completed_at
  FROM requests r
  JOIN target_servers t ON t.id = r.target_server_id
  JOIN target_servers x ON x.id = r.executed_target_id
 WHERE r.executed_at > now() - interval '1 day'
 ORDER BY r.id DESC;
```

**Choosing where one query runs (super-admin).** The web submit takes
`runOn`: `auto` (the rules above, the default), `primary`, or `replica` with a
`replicaId`. That id is optional when the connection has exactly one enabled
replica. QueryHub refuses anyone who is not a super-admin with 403. QueryHub
stores the choice as `requests.run_on` (`NULL`, `'primary'`, `'replica:<target id>'`).

Like `unmasked`, QueryHub verifies the choice again at execution. A requester
who is no longer a super-admin runs as auto, and `execution_started` records
`run_on_ignored`. A scheduled submit keeps its choice. QueryHub verifies the
requester's standing when the scheduler runs it, not at submit time.

- `primary` never consults a replica, at any tier. On SQL Server, a read skips
  the availability group's readable secondary and goes to the listener.
- A chosen replica skips `replica_routing`, the node-local rule,
  read-your-writes and the lag limit. This option exists to read a replica's
  own `pg_stat_activity`. Three conditions still apply:
  - It must be an enabled replica of the request's target.
  - The statement must be read-only. The submit refuses anything else
    with 400.
  - A fresh probe must find the replica answering and in recovery. The probe
    does not need the primary: while the primary is down, the lag is the
    replica's replay age.
- If a chosen replica cannot run the query, before or during the run, the
  request fails. The message names the replica and the reason. The request
  never runs on the primary instead. To exclude a replica from chosen runs as
  well as from automatic ones, disable its row.

Every honoured choice writes an `execution_run_on_forced` audit row in the
claim's transaction: `requested`, `ran_on` (`primary` / `replica`),
`target_id`, `target` and `lag_s`. `GET /api/queries/<id>` and every
`GET /api/history` row carry `ranOn` (`kind`, `name`, `forced`,
`lagSeconds`), read from that row. The web UI builds its "ran on" sentence
from it. The server writes no Messages line and no Slack line for a chosen
node. The "Ran on a read replica ..." line for automatic routing does not
change. `GET /api/connections/<conn>/replicas` shows a super-admin each
replica's health as automatic routing sees it (the cached probe,
`replica_health_ttl_seconds`).

```sql
-- Queries a super-admin sent somewhere on purpose, last week
SELECT a.request_id, a.actor_name, a.details->>'requested' AS requested,
       a.details->>'target' AS ran_on, a.details->>'lag_s' AS lag_s, a.created_at
  FROM audit_log a
 WHERE a.action = 'execution_run_on_forced'
   AND a.created_at > now() - interval '7 days'
 ORDER BY a.id DESC;
```

Masking follows the request's target, the primary, wherever the query runs.
QueryHub finds exemptions and the column catalog by `target_server_id`, never
by the replica's own id. So an exemption written against a replica's
connection row never applies.

**Cancel and lockout.** A cancel signals the backend on the server that runs
the query (`executed_target_id`), with the primary's login.
`scripts/breakglass_lockout.py` ends QueryHub sessions on replicas too. It
writes no role change there: the NOLOGIN reaches a replica through replication.
The script processes the replicas after the primaries.

## 27. Verifying target certificates

`sslmode=require` encrypts a target connection but accepts any certificate.
So a machine in the network path could answer in the server's place, and read
the login and the results. You enable verification per host, so a fleet spread
over two clouds (two certificate authorities) moves one cloud at a time. The
keys are in CONFIGURATION.md, "Target TLS".

**1. Measure without a login.** A TLS handshake is enough to see whether a
CA file verifies a server, host name included:

```bash
openssl s_client -starttls postgres -connect <host>:5432 -servername <host> \
  -verify_hostname <host> -CAfile <ca-file.pem> </dev/null 2>/dev/null \
  | grep "Verify return code"
```

`0 (ok)` means `verify-full` will connect to that host with that file.

**2. Install the CA file** on the QueryHub host, readable by the service user.
For AWS RDS, it is the global bundle, which covers every region:
`https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`. For
another cloud, take the provider's database CA from its console or API, never
from the server itself. A certificate that the server gives you proves nothing
about who the server is. When two clouds verify, put both CAs in one file.

```bash
sudo install -D -m 0644 global-bundle.pem /etc/queryhub/tls/target-ca.pem
```

**3. One host first.** Set `target_ssl_rootcert` to the file and
`target_ssl_verify_hosts` to one host. The settings screen refuses the change
while it cannot read the file. Run a query there, then wait a day.

**4. Then the whole cloud,** for example `*.rds.amazonaws.com`. Each process
logs one line at startup with the number of enabled targets still unverified.

**5. A server that cannot verify** belongs on `target_ssl_verify_exempt_hosts`,
with the reason in its connection notes.

**Undo:** clear `target_ssl_verify_hosts`. The change applies to the next
connection, with no restart.

SQL Server reads the same two lists (`TrustServerCertificate=no` for a listed
host). `mssql_trust_server_cert` covers the hosts that neither list names. A
target reached by IP address verifies only if its certificate names that
address. ClickHouse always verifies, against the public CA bundle.

The metadata DB is not a target. It has its own two settings,
`BOT_DB_SSLMODE` and `BOT_DB_SSLROOTCERT`, in the service environment. A
change there needs a restart of both services, and a wrong value stops both.
So measure it with the command above first. Do not set libpq's `PGSSLROOTCERT`
instead. libpq applies it to every connection that names no root file, which
turns each target's `require` into `verify-ca` against the wrong CA.

## 28. Splitting the metadata database roles

One login owns the metadata database and everything in it. Both services and
the scheduled jobs connect with it. Owning `audit_log` means UPDATE, DELETE
and TRUNCATE on it, whatever the code does. So a leaked runtime credential
could rewrite the audit trail. The split withdraws that power without a change
to the services' configuration: the runtime keeps its login and password.

| Role | Login | Holds |
|---|---|---|
| owner (e.g. `queryhub_owner`) | no | the database and every object in `public` |
| migrator (e.g. `queryhub_migrator`) | yes | membership in owner. Only `scripts/apply_migrations.py` uses it |
| runtime (`BOT_DB_USER`, unchanged) | yes | DML on tables, SELECT on views, USAGE on sequences, CONNECT and TEMPORARY on the database. On `audit_log`, only SELECT and INSERT. On `schema_migrations`, nothing |

Code: `src/queryhub/metadata_roles.py` (the policy),
`scripts/split_metadata_roles.py` (the change). CI runs the whole integration
suite a second time as a split runtime. So a code path that needs more than
DML fails there first.

What the runtime needs, and why each piece is there:
- **TEMPORARY on the database.** The access-model mirror trigger (migration
  109) creates a temporary table on every write to the legacy access tables.
  That includes the profile refresh on each `/sql` submission.
- **SELECT only on views.** A simple view such as `audit_log_reportable` is
  auto-updatable. PostgreSQL verifies an UPDATE through it against the view's
  owner. A blanket grant on all tables includes views. It would leave the
  audit rows editable indirectly, through a view.
- **INSERT and USAGE on `audit_log`'s sequence.** The `bot_config` audit
  trigger (migration 066) writes it as the invoker.

**1. Read the plan.** The command is read-only, and it locks nothing:

```bash
PGPASSWORD=... python scripts/split_metadata_roles.py --owner queryhub_owner --admin-user <admin login>
```

The admin login is the database's administrative user (the RDS master), not
the runtime. The runtime is the role that loses privileges, and it cannot
transfer them.

**2. Create the two roles**, as that admin login. You choose the migrator's
password, and it never passes through the script:

```sql
CREATE ROLE queryhub_owner NOLOGIN;
CREATE ROLE queryhub_migrator LOGIN IN ROLE queryhub_owner;
\password queryhub_migrator
-- ALTER ... OWNER needs the admin login to be able to SET ROLE to both:
GRANT queryhub_owner TO <admin login>;
GRANT <runtime login> TO <admin login>;
```

Put the migrator's login in its own file, readable by the operator only, and
never in the services' environment:

```bash
# /etc/queryhub/migrator.env, mode 0600
BOT_DB_MIGRATOR_USER=queryhub_migrator
BOT_DB_MIGRATOR_PASSWORD=...
BOT_DB_OWNER_ROLE=queryhub_owner
```

**3. Rehearse.** `--rehearse` runs everything in one transaction, verifies
it, and rolls back. It takes an ACCESS EXCLUSIVE lock on every table for the
second or two it runs. So run it at a quiet moment, or on a restored copy.

**4. Apply.** `--apply` runs the same transaction. It commits only if all of
these hold:

- nothing left owned by the runtime
- INSERT but no UPDATE, DELETE or TRUNCATE on `audit_log`
- UPDATE still on `requests`
- TEMPORARY held
- no CREATE on `public`, and no view writable

If another session is using a table, the script aborts after 3 seconds. Run it
again. No restart is needed.

**5. Test the live paths** once: a `/sql` read, a web submit, an admin
button. Each one exercises a trigger.

**6. Migrations from now on** run as the migrator:

```bash
set -a; . /etc/queryhub/migrator.env; set +a
python scripts/apply_migrations.py
```

Without those variables, the runner says the ledger is unreadable and stops.
After the last file, it re-applies the runtime's grants, so a new table gets
DML and a new view gets SELECT only. The container entrypoint unsets the three
variables after it migrates. With a separate one-off migration container, the
service container never holds them at all.

**Undo:** `--rollback --apply` returns everything to the runtime login, which
is the state before the split.

Not done by the split, and still open (SEC-AUDIT):

- A hash chain over `audit_log`, with an anchor outside the database.
- `audit_log.request_id`'s `ON DELETE
  SET NULL`, which lets a DELETE on `requests` rewrite audit rows through the
  foreign key. No runtime path deletes an audited request today. Drafts are
  the only requests that the runtime deletes, and they have no audit rows.
