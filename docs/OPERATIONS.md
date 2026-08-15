# QueryHub — operations cheatsheet

All CLI commands, DB configs, and admin SQL snippets in one place.
Copy-paste ready. Most commands assume `set -a; source /etc/slackbot/env; set +a`
(needed by anything that connects to the bot DB). Each section is
self-contained — read what you need, skip the rest.

> **Not in this document:** backing up and restoring the control plane, and
> what happens if you lose the master key — [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md).
> Replacing the master key without downtime — [KEY_ROTATION.md](KEY_ROTATION.md).
> What personal data the system stores, for how long, and how to answer a
> data-subject request — [COMPLIANCE.md](COMPLIANCE.md).

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

---

## 1. Bot lifecycle

```bash
# Status / health
systemctl is-active slackbot
systemctl status slackbot --no-pager | head -15

# Restart (after any code or env change)
sudo systemctl restart slackbot

# Live logs
sudo journalctl -u slackbot -f

# Last 50 lines
sudo journalctl -u slackbot -n 50 --no-pager

# Errors only, last hour
sudo journalctl -u slackbot --since "1 hour ago" --no-pager | grep -iE "ERROR|WARN"
```

**Service file**: `/etc/systemd/system/slackbot.service` →
`WorkingDirectory=<repo-path>`, `EnvironmentFile=/etc/slackbot/env`,
runs as the user the repo is owned by (whoever you chose at install
time). See `deploy/INSTALL.md` for the placeholder substitution.

**Update flow**: `cd <repo-path> && git pull && sudo systemctl restart slackbot`.

---

## 2. Encrypted secrets

The 3 secrets (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `BOT_DB_PASSWORD`) live
encrypted in `/etc/slackbot/secrets.enc` (Fernet via `master.key`). The
plaintext `/etc/slackbot/env` no longer contains any secrets.

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
sudo systemctl restart slackbot
```

File format: line 1 is `SLBOT_SECRETS_v1` (signature for human
identification), line 2 is the Fernet ciphertext. Permissions enforced
to `0600`; the loader rejects any group/other bits.

---

## 3. Bot config knobs (bot_config table)

All runtime tunables live here. Edited via SQL; bot reads on each
relevant operation (no restart needed unless noted).

```sql
-- See everything:
SELECT key, value, left(description, 100) AS desc FROM bot_config ORDER BY key;

-- Update a single key:
UPDATE bot_config SET value = '<new>' WHERE key = '<key>';
```

| Key | Default | Restart? | What it does |
|---|---|---|---|
| `bot_display_icon` | `:query_hub:` | no | Emoji used as bot avatar in chat. Needs to be uploaded as a custom emoji in the workspace. |
| `bot_display_name` | `QueryHub` | no | Username shown in chat |
| `csv_size_mb` | `10` | no | Max CSV file size; result-streaming aborts at this cap |
| `kill_switch` | `off` | no | Master kill switch. `on` blocks new submissions + scheduler dispatch (admin approve still works) |
| `kill_switch_message` | (banner text) | no | Ephemeral message shown when kill_switch is on |
| `log_level` | `INFO` | yes | Python logging level |
| `max_open_requests_per_user` | `5` | no | Cap on in-flight requests per non-admin Slack user (pending/approved/scheduled/executing) |
| `max_rows` | `1000` | no | Max rows returned in CSV |
| `max_schedule_days` | `7` | no | Max future days for `/sql` scheduling. Set `0` to disable scheduling |
| `min_query_length` | `6` | no | Reject queries shorter than this |
| `pre_flight_explain` | `off` | no | Run EXPLAIN at modal-submit time to catch typos. RO queries only — RW/DDL skipped automatically |
| `query_plan_logging` | `off` | no | When pre_flight_explain is on AND this is on, store EXPLAIN plan in `requests.explain_plan` (capped at 64KB) |
| `query_timeout_sec` | `300` | no | Per-query `statement_timeout` (5 min) |
| `require_justification` | `false` | no | Require justification field even for RO queries (RW/DDL always require it) |
| `results_ttl_hours` | `72` | no | How long Slack/local CSV results are kept before cleanup deletes. Kept short to limit how long sensitive result data lives in Slack |
| `rating_enabled` | `on` | no | Post a 1-5 rating prompt DM after every terminal-state request (suppressed 30 days after a user's most recent rating). See section 13 |
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

Idempotent SQL files under `migrations/`. Numbered sequentially.

```bash
# Apply all (skips already-applied; commits per file)
.venv/bin/python scripts/apply_migrations.py

# What's applied (the script doesn't track state itself; rely on
# migration files being idempotent — INSERT ... ON CONFLICT DO NOTHING,
# CREATE TABLE IF NOT EXISTS, ALTER TABLE ... ADD COLUMN IF NOT EXISTS).
ls migrations/
```

To add a new migration:
1. Create `migrations/NNN_name.sql` with idempotent DDL/DML
2. Run `apply_migrations.py`
3. Commit the file

Current migration count: 35 (last: `035_report_start_date.sql`).

---

## 5. Targets — add / disable / rotate

Target = a Postgres cluster the bot can query. Stored in `target_servers`
with Fernet-encrypted credentials per tier (RO required, RW/DDL optional).

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
     'dba_slackbot_ro',
     '<paste Fernet ciphertext from encrypt_secret.py>',
     TRUE,
     'orders cluster, prod');
```

### Add RW credentials to an existing target

```sql
UPDATE target_servers
   SET username_rw         = 'dba_slackbot_rw',
       password_rw_encrypted = '<paste Fernet ciphertext>'
 WHERE alias = 'acme-prod-orders';
```

### Add DDL credentials (when first DDL request arises)

```sql
UPDATE target_servers
   SET username_ddl         = 'dba_slackbot_ddl',
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
# Adds enabled rows with sentinel 'PASSWORD_NOT_SET' password — visible
# in modal but fails fast at execute time until you UPDATE real creds.
```

---

## 6. Admins

Admins approve/reject requests. They bypass team grants and
allowlist; queries run against any enabled target.

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

`name` and `email` get auto-refreshed from Slack on every interaction
(profile_sync). You can leave them blank on insert.

---

## 7. Requesters (allowlist + bypass)

The allowlist for `/sql`. If table is empty (zero enabled rows), the
bot is OPEN to everyone in the workspace. Otherwise only enabled
requesters can submit.

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

`bypass_team_grants = TRUE` makes the user see every enabled target
(admin-equivalent visibility) and submit any tier (synthetic
'ddl' grant on everything). They are still NOT admin — cannot
approve/reject.

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

Teams own targets (via `team_target_grants`). Users belong to teams (via
`team_members`). User effective grant = most-permissive across their
team grants on a given target.

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

`target_role` lets the executor `SET LOCAL ROLE <name>` for queries from
this team — extra defense layer at the cluster level. Optional but
recommended for RW/DDL tier; not yet provisioned for the pilot.

```bash
# Generate runbook for currently-unprovisioned (team, target) pairs:
.venv/bin/python scripts/plan_team_role_provisioning.py > /tmp/runbook.md
```

The runbook outputs psql commands to run on each target cluster, plus
the `UPDATE team_target_grants SET target_role = ...` statement to wire
it on the bot side.

---

## 9. Per-user grants (overrides)

`user_target_grants` overrides team grants for a specific (user, target).
Use sparingly; prefer team grants.

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
ls -la /etc/slackbot/master.key   # mode 600 ubuntu:ubuntu, 44 bytes

# Fingerprint sidecar (optional sanity check)
cat /etc/slackbot/master.key.fingerprint

# First-time generation (one-shot, with backup ritual)
sudo .venv/bin/python scripts/init_master_key.py

# Encrypt a single secret (for INSERT into target_servers.password_*)
.venv/bin/python scripts/encrypt_secret.py
```

The same key:
- Decrypts `target_servers.password_encrypted` (and `_rw_encrypted`,
  `_ddl_encrypted`)
- Decrypts `/etc/slackbot/secrets.enc` (Slack tokens + bot DB password)

**If you rotate this key, you must also re-encrypt every dependent
secret with the new key.** No tooling for that today; do not rotate
casually.

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
du -sh /var/lib/slackbot/results /var/log/slackbot 2>/dev/null
```

### Forced shutdown if systemctl restart hangs

```bash
sudo systemctl stop slackbot           # waits up to 90s
sudo systemctl kill --signal=SIGKILL slackbot
sudo systemctl start slackbot
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
`cancelled`) the bot DMs the requester a 1-5 rating prompt. A user who
has rated anything in the last 30 days is silently skipped (cooldown).
Low ratings (1-2) get a contextual "What went wrong?" follow-up button
that opens a feedback modal; high ratings get an "Add feedback"
button. One rating per request — first click locks.

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

Users may submit multiple `;`-separated statements in one /sql request,
optionally preceded by `SET LOCAL <param> = <value>` lines. Rules:

- All non-SET ("main") statements must be the **same tier** (all RO, or
  all RW, or all DDL). Mixing — e.g. `SELECT 1; UPDATE t SET x=1 WHERE id=1`
  — is rejected with a "mixed-tier" error.
- `SET ...` is auto-rewritten to `SET LOCAL ...` (transaction-scoped).
- Only parameters in `bot_config.set_allowed_params` are accepted;
  others rejected with a friendly error.
- SET must come BEFORE any main statement — trailing SETs rejected.
- For multiple read result sets, each SELECT writes its own CSV; bot
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
enable_*, random_page_cost, ...). To add or remove:

```sql
-- See current
SELECT value FROM bot_config WHERE key = 'set_allowed_params';

-- Update (comma-separated, no spaces necessary)
UPDATE bot_config
   SET value = 'work_mem,statement_timeout,enable_seqscan,...'
 WHERE key = 'set_allowed_params';
```

Bot reads on every submission — no restart needed.

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

Thirteen read-only views in the bot DB. All carry the `p_metrics_`
prefix; cost numbers are tunable via `bot_config.cost_*` rows.

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

Every admin starts as a "super admin" (can approve every request).
Three optional scope columns narrow that authority — set any, all, or
none. NULL on a column means "no restriction in that dimension".

| Column | Type | Meaning when NULL | Meaning when set |
|---|---|---|---|
| `max_tier` | text | Approves any tier | `'ro'` / `'rw'` / `'ddl'` — highest tier this admin can approve (hierarchy: ro < rw < ddl) |
| `scope_target_ids` | int[] | Any target | Only requests on these `target_servers.id` values |
| `scope_team_ids` | int[] | Any requester team | Only requests from a requester who is a member of at least one of these teams |

Resolution goes through a single function (`admins.can_approve`) — all
button checks, DM-button visibility, and modal-submit guards use it.
When an out-of-scope admin clicks anyway, they're rejected with a DM:
"This request is outside your admin scope (tier / target / team)."

Out-of-scope admins still receive the request DM (audit + transparency)
but with a "view only" footer instead of Approve / Reject / Request
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
  every admin has a non-NULL scope, some requests may end up with
  no eligible approver and stay pending.
- **No-team requesters** are not matched by a non-NULL
  `scope_team_ids`. If you scope an admin to `team_ids`, that admin
  cannot approve requests from standalone users (those with only
  `user_target_grants`). Either widen the admin's scope, or grant a
  super admin alongside.
- **Tier changes when a query is edited** through Request-changes.
  The bot re-classifies on resubmit; the new admin scope check
  applies to the new tier.

---

## 17. Keeping real identifiers out of what you share

Almost everything QueryHub puts on a screen names something real: a
connection alias, a database, a hostname in an error message, the person
who asked. That is the point in normal use, and a problem the moment any
of it leaves the deployment — a screenshot in a ticket, a log excerpt in
a chat, a config snippet in a bug report upstream.

Four places it leaks most easily:

- **The connection list and the audit log.** Both are alias-dense by
  design. Crop or redact before pasting.
- **Error text.** `errors.py` scrubs libpq messages before a user sees
  them, but the unscrubbed original is in the service log.
- **Result files.** A CSV or XLSX under `QH_RESULTS_DIR` is real data
  until the retention job removes it (`results_ttl_hours`).
- **A fork of this repository.** If you commit your own `bot_config`
  rows, migrations or fixtures, the aliases and user ids go with them.

If you maintain a fork, the practice worth copying is a pre-commit check
that builds its denylist *from the metadata database* rather than from a
hand-kept list — every alias, host, team name and user id it currently
holds — so the list cannot go stale as people and targets come and go.
Static patterns alone (token shapes, private keys, RFC1918 addresses)
will not catch the thing most likely to leak, which is a name.


## 18. Batch submissions (`/sql batch`)

The bot lets a user submit up to N queries in one approval round.
Each item becomes its own `requests` row, linked by `bundle_id`.
Per-item Approve / Reject / Changes buttons re-use the existing
handlers; "Approve all remaining" / "Reject all remaining" buttons
collapse a single admin's pending items in one click. A single
summary DM lands once the whole bundle is decided + executed, with
every completed item's CSV attached.

### Feature flag

```sql
-- Turn on (off by default — modal toggle + sub-command hidden when off).
UPDATE bot_config SET value = 'on' WHERE key = 'batch_enabled';

-- Cap the max items per bundle.
UPDATE bot_config SET value = '5' WHERE key = 'batch_max_items';
```

### How users access it

- `/sql` → modal shows a *Single ↔ Batch* radio toggle at the top
  when `batch_enabled = 'on'`. Switching preserves whatever the user
  already typed (single's query becomes batch item #1 and vice versa;
  batch → single warns when items #2+ are dropped).
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

`requests.status` changes fire an AFTER UPDATE trigger
(`trg_recompute_bundle_status`) that recomputes `request_bundles.status`
using `pg_advisory_xact_lock(bundle_id)` to serialise concurrent
recomputes. The rule set:

| Item mix | Bundle status |
|---|---|
| any pending / approved / scheduled / executing / awaiting_dba_manual / changes_requested | `pending` |
| all `cancelled` | `cancelled` |
| at least one `completed` AND at least one terminal-negative | `partial` |
| else (all completed / all rejected / all failed) | `decided` |

### Summary DM idempotency

`request_bundles.requester_summary_message_ts` records the Slack ts of
the requester's bundle-summary DM. First time the bundle reaches a
terminal state → DM posted + ts saved. Subsequent state changes
(e.g. a manually-completed DDL item closed hours later) `chat.update`
the same DM rather than posting a new one.

---

## 19. Auto-approve grants

Per-user, time-bounded, tier-scoped exemption from admin approval.
The bot evaluates grants at submit time AND at scheduled run time
(if scheduled_for is set) — a grant that's active now but expires
before the run falls back to admin approval with a user-facing warning.

### Schema cheat sheet

```sql
\d auto_approve_grants
\d v_active_auto_approve
```

`max_tier` is `ro` / `rw` / `ddl`. RO covers RO only; RW covers RO+RW;
DDL covers everything. Queries above the grant's tier route through
the normal admin flow.

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

- Modal banner (":zap: Auto-approve active — up to RO, until …")
  appears at the top of the `/sql` modal whenever the user has any
  active grant.
- Auto-approved requests INSERT with `status=approved` (or
  `'scheduled'`), `decided_by_slack_id='AUTO'`, and a
  `decided_by_name` like `auto-approved (grant #N, max_tier=ro, until ...)`.
- A short FYI DM lands on every active admin per auto-approved
  request — header + the inline query (truncated at 500 chars).
  Bundle FYI batches all auto-approved items in one DM per admin.
- Higher-tier queries (e.g. user has RO grant, submits RW) fall
  back to the standard pending → admin approval flow. No silent
  privilege escalation.
- Scheduling guardrail: if `scheduled_for` lands AFTER `expires_at`,
  the submit handler falls back to admin approval and warns the
  user in the confirmation DM.

---

## 20. Milestone annotations

`metric_annotations` is a free-form table for marking notable
moments on the product-metrics timeline (go-live, access cutover,
incident, config change). The `p_metrics_usage_daily` view joins
these by day so a dashboard can label its bars.

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

UNIQUE(`occurred_at, label`) so the migration seed is safe to re-run.



## 21. Temporary admin grants (vacation / on-call coverage)

Time-bounded admin role. A **super-admin** (a permanent admin with
ALL scope columns NULL) can deputise someone for a defined window;
the deputy automatically loses admin status the moment
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

The bot keeps the permanent `admins` table immutable — temp grants
go into `temp_admin_grants`, and `is_admin` / `can_approve` /
`list_active` consult both tables.

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
from dba_slack_bot import admins
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
- Admin DM "Approved by @user @ `2026-05-20 17:38 UTC`" doesn't
  distinguish permanent vs temp — slack id is enough for audit; the
  `temp_admin_grants` table holds the per-grant context.


## 22. Excluding test traffic from product metrics

Operator self-tests would otherwise pollute every `p_metrics_*` view
(volume, top users, admin workload, ratings, cost savings). The
`report_excluded_users` table is an allowlist-of-exclusions consulted
by three thin wrapper views (`requests_reportable`,
`audit_log_reportable`, `request_ratings_reportable`); every metric
view reads from those instead of the raw base tables. The bot's
runtime paths (kill-switch, allowlist, team grants, admin scope,
audit_log) all keep reading the raw tables — exclusion is metrics-only.

### Filter semantics

| View family | Filter applied |
|---|---|
| `requests_reportable` | drops rows where `requester_slack_id` is in `report_excluded_users` |
| `audit_log_reportable` | drops rows whose linked `request_id` is itself dropped from `requests_reportable`. Actor identity alone never excludes — an excluded user's approvals of OTHER people's real requests stay visible in admin reports (so their actual DBA workload is preserved). |
| `request_ratings_reportable` | drops ratings whose rater is excluded OR whose underlying request was dropped from `requests_reportable` |

So an excluded user's **own** requests (and any audit actions on
them) vanish from the reports. Actions the excluded user took on
OTHER people's real requests stay visible — admin reports reflect
the user's actual DBA workload, just minus the self-test noise.

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

Changes take effect on the next view read — no restart, no
re-aggregation, no migration.



## 23. Publishing the metrics dashboard to S3

The dashboard HTML (`metrics_dashboard.html`) is regenerated and
uploaded to an S3 bucket on a systemd timer. Browsers hit a fronted
URL (private ALB / CloudFront with auth) → S3 → fresh-ish HTML
(refresh interval = timer cadence, default hourly).

### One-time setup

**What has to exist before the upload works:**

1. Private S3 bucket — e.g. `<company>-internal-dba-dashboards`.
   Block-Public-Access ON; no public listing.
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
   user's `~/.aws/credentials`. The role approach is preferred —
   no key to rotate.)
3. Fronting:
   - **CloudFront distribution** with an Origin Access Control (OAC)
     reading the bucket privately, OR
   - **Private ALB** with an S3 VPC endpoint.
4. **TLS cert** on the front (ACM).
5. **Internal DNS** record (e.g.
   `dba-metrics.<company-internal>.com`).
6. **Authentication** — minimum is Cognito (IdP-backed) at the
   CloudFront / ALB layer. Anonymous internal access is also
   acceptable on a tight VPN, but adds a rotation-of-trust step.

### Bot-side wiring

Once the bucket exists, the operator's side is three files (already
in the repo):

```bash
# 1. Bucket name + key (key is optional; default is dba-metrics/index.html).
sudo tee /etc/slackbot/dashboard.env >/dev/null <<EOC
METRICS_DASHBOARD_BUCKET=<the-bucket-name>
METRICS_DASHBOARD_KEY=dba-metrics/index.html
AWS_REGION=eu-central-1
EOC
sudo chown __USER__:__USER__ /etc/slackbot/dashboard.env
sudo chmod 600 /etc/slackbot/dashboard.env

# 2. Drop the systemd unit + timer in place with placeholders
#    substituted (same pattern as slackbot.service in INSTALL.md).
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

- HTML size: ~50 KB; 24 uploads/day × 50 KB ≈ 1.2 MB/day → bucket
  storage ≈ negligible.
- `Cache-Control: max-age=300, must-revalidate` — readers see a fresh
  copy within 5 minutes of the next upload, even through CloudFront.



---

## 24. Monitoring: `/metrics` and structured logs

Two things an operator needs and neither existed: nothing to scrape, and log
lines only a regex could parse.

### `GET /metrics`

Prometheus text format, **off by default**. A self-hosted tool should not start
publishing its queue depth, fleet size and user counts because somebody
upgraded, so turning it on is a decision:

```sql
UPDATE bot_config SET value = 'on' WHERE key = 'web_metrics_enabled';
```

Runtime-effective — no restart. While off the route answers **404**, not 403; a
403 would confirm the endpoint is there.

For a scraper, set a token as well — Prometheus can send a bearer header but
cannot hold a session cookie:

```sql
UPDATE bot_config SET value = 'PASTE_TOKEN' WHERE key = 'web_metrics_token';
```

Generate it like any other credential:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

With the token empty, the endpoint requires an **admin session** instead — so
enabling the key alone does not publish it. The token is compared in constant
time.

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
is deliberate: counters in memory reset on restart, and the two processes
(`slackbot` and `queryhub-web`) each see only part of the traffic, so neither
would have the whole picture. Counters here are cumulative over all history,
which is what `rate()` expects.

### What is worth alerting on

| Metric | Why |
| --- | --- |
| `queryhub_oldest_request_age_seconds{status="pending"}` | **The one that matters.** Depth alone cannot tell "three arrived this second" from "one has waited since yesterday". This is what a developer experiences as the tool being broken. |
| `queryhub_requests_in_state{status="executing"}` | Stuck executions. Should return to 0; a value that never falls means a lease is not being released. |
| `queryhub_kill_switch_active` | 1 means all new query traffic is halted. Easy to leave on after an incident. |
| `queryhub_auth_event_outbox_depth` | Monotonic growth means the auth-event poller is not running — grant changes are being recorded and never announced. This is a real failure mode: the poller once lived only in the Slack process, so a vanilla install grew this table forever and nothing said so. |
| `queryhub_scrape_errors` | Non-zero means some collector failed and the numbers are partial. A dashboard of zeros because every query broke looks exactly like a healthy idle system. |
| `rate(queryhub_requests_total{status="failed"}[15m])` | Execution failures. |

Durations are exposed as `_sum`/`_count` pairs, so
`rate(queryhub_execution_seconds_sum[1h]) / rate(queryhub_execution_seconds_count[1h])`
gives the average over whatever window you pick. There are no quantiles: real
ones need histogram buckets kept in process memory, and per the above there is
no process memory to keep them in.

One failing collector does not fail the scrape — a partial payload beats a 500
at the exact moment something is already wrong.

### Structured logs

`LOG_FORMAT=json` in the service environment switches both processes to one JSON
object per line:

```json
{"timestamp":"2026-07-25T15:26:01+00:00","level":"INFO","logger":"queryhub.executor","message":"executing request 4242 on target demo-primary","request_id":4242,"tier":"ro"}
```

`timestamp` is RFC 3339 in **UTC** regardless of `web_display_timezone` —
correlating two hosts across a DST boundary is the kind of problem that costs an
hour at 3am. Tracebacks stay on one line as an `exception` field, which is the
main reason to switch: in text format a traceback arrives at the log backend as
N unrelated lines, and the one naming the exception is not the one with the
context.

`text` remains the default, because a human tailing `journalctl` is the common
case and JSON is worse for that. Both this and `LOG_LEVEL` are read once at
process start, so a change needs a restart:

```bash
sudo systemctl restart slackbot queryhub-web
```
