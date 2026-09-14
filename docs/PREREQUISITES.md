# Prerequisites

Everything you need to have ready **before** running through
`deploy/INSTALL.md`. Allow ~30 minutes for the Slack-side setup and
~15 minutes for the DB-side setup if you're starting from scratch.

> **No Slack? Skip section 1 entirely.** The **vanilla profile** runs
> QueryHub web-only: base install (`pip install .`), built-in local
> accounts for login (`scripts/create_local_user.py`), approvals in the
> web admin panel. Only sections 2–5 apply. The Slack surface can be
> added later — it is purely additive (`pip install '.[slack]'` + the
> section-1 app).

> **Surfaces & engines.** Section 1 covers the **Slack** surface. The
> **web UI** (QueryHub Web) is a separate process on the same core
> (`python -m dba_slack_bot.web`, FastAPI + TLS); with Slack installed you
> can log into it via Slack OIDC, without Slack via local accounts. Target
> databases can be **PostgreSQL or SQL Server**; SQL Server targets also need
> the Microsoft ODBC driver (`msodbcsql18`) + the `mssql` extra
> (`pip install '.[mssql]'`).

## Contents

1. [Slack app](#1-slack-app)
2. [PostgreSQL — bot metadata DB](#2-postgresql--bot-metadata-db)
3. [PostgreSQL — target clusters](#3-postgresql--target-clusters)
4. [Linux host](#4-linux-host)
5. [Network](#5-network)
6. [Optional integrations](#6-optional-integrations)

---

## 1. Slack app

The bot runs as a custom Slack app in Socket Mode (no public endpoint
needed). Create the app once per workspace.

### Step-by-step

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create
   New App** → **From scratch**. Name it whatever you want (this
   becomes the display name in chat, e.g. `QueryHub`); pick the
   target workspace.

2. **Socket Mode** → enable it. Click **Generate Token and Scopes**.
   Add scope: `connections:write`. Save the resulting **App-Level
   Token** (`xapp-…`) — you'll put this in the bot's secrets.

3. **OAuth & Permissions** → **Scopes** → **Bot Token Scopes**, add:

   | Scope | Why |
   |---|---|
   | `chat:write` | Post DMs to admins + requesters |
   | `chat:write.customize` | Override `username` / `icon_emoji` on `chat.postMessage` so the bot shows up as the configured display name |
   | `commands` | Register the `/sql` slash command |
   | `users:read` | Look up name / email / timezone for `profile_sync` |
   | `im:write` | Open DM channels with users on the fly |
   | `files:write` | Upload result CSVs |

4. **Slash Commands** → **Create New Command**:
   - Command: `/sql`
   - Short description: e.g. *"Submit a SQL query for admin approval"*
   - Usage hint: optional
   - Request URL: leave blank (Socket Mode handles it)

5. **Interactivity & Shortcuts** → **Interactivity**: ON.
   Request URL: leave blank.

6. **App Home** → **Show My Bot as Online** if you want; set the bot
   user's display name here (this is the identity Slack shows on file
   uploads, where `chat:write.customize` can't override). Recommended
   to match the bot's intended brand name.

7. **Install App** to your workspace. After install, copy the **Bot
   User OAuth Token** (`xoxb-…`) — you'll put this in the bot's
   secrets alongside the app-level token.

### What you walk away with

- `SLACK_BOT_TOKEN` (`xoxb-…`)
- `SLACK_APP_TOKEN` (`xapp-…`)
- Workspace admin install consent (one-time)

Both tokens will be stored encrypted in `/etc/slackbot/secrets.enc`
via `scripts/manage_env_secrets.py init` during install.

---

## 2. PostgreSQL — bot metadata DB

A single Postgres database the bot uses for its own state: requests,
audit log, target registry, config, ratings, etc.

### Requirements

- PostgreSQL **14+** (uses `pg_read_all_data`, `pg_write_all_data`,
  PG14 predefined roles; jsonb; partial indexes; CHECK constraints).
- Network reachable from the bot host on port 5432 (or your custom
  port) over TCP.
- Roughly **2-5 GB** disk to start; grows with `audit_log` +
  `requests.explain_plan` (capped per row).
- A dedicated database is recommended (e.g., `slackbot` on a shared
  Postgres host), but the bot can also share a cluster with other
  apps as long as it owns its own DB.

### One-time bootstrap (you'll run this during install)

You need a Postgres admin user — `postgres`, `rds_superuser`, or any
role that can `CREATE DATABASE` + `CREATE ROLE` — to run
`deploy/setup_db.sql` once. That script:

- Creates the `slackbot` login role (NOSUPERUSER, NOCREATEDB,
  NOCREATEROLE, connection-limit 20)
- Creates the `slackbot` database, owned by the `slackbot` role
- Grants nothing else — the bot is self-contained inside its own DB

After this, the admin role is no longer needed.

---

## 3. PostgreSQL — target clusters

The Postgres servers your developers actually want to query.

### Requirements

- Postgres **14+** on every cluster.
- Network reachable from the bot host on port 5432 (TLS recommended
  — the bot uses `sslmode=require` on every target connection).
- Ability to **create login roles** on each cluster (or have an admin
  who can run the bootstrap SQL once per target).

### Per-target setup

For each cluster you want exposed via `/sql`, provision **at least
one** login role:

| Role | Required? | Privileges | Used for |
|---|---|---|---|
| `dba_slackbot_ro` | **yes** | `pg_read_all_data` (PG14+) | RO queries (SELECT/EXPLAIN/etc.) |
| `dba_slackbot_rw` | optional | `pg_read_all_data` + `pg_write_all_data` | RW queries (INSERT/UPDATE/DELETE/MERGE) |
| `dba_slackbot_ddl` | optional | granular DDL grants (see below) or `rds_superuser` | DDL (CREATE/ALTER/DROP). The bot escalates owner-only ops to manual DBA execution, so you can keep this role narrow |

Helper script: `deploy/grant_readonly.sql` provisions `dba_slackbot_ro`
in a single database. Run once per database on each target.

The bot stores each role's password encrypted (Fernet) in
`target_servers.{password_encrypted, password_rw_encrypted,
password_ddl_encrypted}`. Per-target rotation is a one-liner UPDATE
after re-encrypting with `scripts/encrypt_secret.py`.

### DDL grants — the narrow path

If you want DDL execution but don't want to give `rds_superuser`, the
typical granular grant set is:

```sql
CREATE ROLE dba_slackbot_ddl WITH LOGIN PASSWORD '...'
    NOSUPERUSER NOCREATEDB NOREPLICATION CONNECTION LIMIT 5;

GRANT CONNECT ON DATABASE <db>     TO dba_slackbot_ddl;
GRANT USAGE   ON SCHEMA public     TO dba_slackbot_ddl;
GRANT CREATE  ON SCHEMA public     TO dba_slackbot_ddl;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public TO dba_slackbot_ddl;
GRANT USAGE   ON ALL SEQUENCES IN SCHEMA public TO dba_slackbot_ddl;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON TABLES TO dba_slackbot_ddl;
```

This lets the role CREATE new objects + do all DML on existing ones,
but **not** ALTER / DROP / CREATE INDEX on tables it doesn't own. Such
operations transition the request to `awaiting_dba_manual`; a human
DBA finishes them out-of-band and closes the request from Slack.

---

## 4. Linux host

The bot is a long-running daemon; a small VM or container is enough.

### Requirements

| | |
|---|---|
| OS | Linux with systemd. Ubuntu 22.04+ tested; any modern distro works |
| CPU / RAM | 1 vCPU / 512 MB is plenty for a small org; bumps as request volume grows |
| Disk | ~2 GB for the venv + CSV results buffer (`/var/lib/slackbot/results`); CSVs auto-cleanup per `results_ttl_hours` |
| Python | `python3.11` + `python3.11-venv` + `python3-pip` |
| System packages | `libpq-dev`, `git` |
| Sudo | Needed during install to create `/etc/slackbot`, `/var/lib/slackbot`, `/var/log/slackbot` and to install the systemd unit |
| User | A normal Linux user (e.g. `ubuntu`) that owns the repo and the runtime directories. Service runs as this user; not root |

### Filesystem layout (created during install)

```
/etc/slackbot/
├── env               # non-secret config (mode 600, owner $BOT_USER)
├── master.key        # Fernet master key (mode 600, owner $BOT_USER)
├── master.key.fingerprint
└── secrets.enc       # encrypted Slack tokens + bot DB password

/var/lib/slackbot/results/   # CSV results, auto-cleaned per TTL
/var/log/slackbot/           # not currently used; reserved for future
```

---

## 5. Network

| | |
|---|---|
| Outbound HTTPS to `slack.com` | Required for Socket Mode WebSocket + the Web API |
| Outbound TCP to bot metadata DB | Port 5432 (or your custom) |
| Outbound TCP to every target DB | Port 5432 each. If you're on AWS with private subnets, the bot host's security group must reach each target's SG |
| Inbound | **None.** Socket Mode means the bot has no public endpoint and accepts no incoming connections |
| DNS | Standard outbound DNS resolution |
| Time | NTP synced (Slack signs requests with timestamps; large drift breaks Socket Mode) |

---

## 6. Optional integrations

These are nice-to-haves; the bot runs fine without them.

### Inventory view (for bulk target import)

`scripts/import_targets_from_inventory.py` and the hourly sync
wrapper read from a view `inventory.v_all_databases(endpoint, database_name)`
in your bot DB (or any DB the bot can reach). If you populate this view
from your own inventory source, the bot auto-discovers new endpoints
and disables decommissioned ones. Without it, you add targets manually
via the SQL templates in `deploy/db_admin_templates.sql`.

Suggested shape:

```sql
CREATE VIEW inventory.v_all_databases AS
SELECT endpoint, database_name FROM (your inventory source);
```

### Scheduled cleanup

`scripts/cleanup_old_results.py` deletes local CSV files and Slack
file uploads older than `results_ttl_hours` (default 72h). Schedule
it daily via:

- a systemd timer (template in `deploy/INSTALL.md` section 12), **or**
- a plain cron entry, **or**
- your existing job runner

### Log correlation

For audit / debugging, set `log_line_prefix` on each target RDS
parameter group to include the application name. Suggested:

```
%m [%p] %q%u@%d %r %a %x %e %i
```

The bot sets `application_name=slackbot req=<id> by=<email-or-slack-id>`
on every target connection, so this prefix makes "which request ran
this query" visible in the target's Postgres log alongside the
query itself.

### Per-team Postgres role enforcement

For defense-in-depth beyond the bot's application-layer team grants,
provision a `slackbot_team_<name>` role on each target with team-scoped
SELECT/INSERT/etc. privileges. Set `team_target_grants.target_role`
to the role name, and the bot will `SET LOCAL ROLE <role>` before
running the query — so Postgres enforces team boundaries natively.

Helper: `deploy/grant_team_role.sql` plus
`scripts/plan_team_role_provisioning.py` (generates a runbook for
unprovisioned (team, target) pairs).

---

Once all of the above are in place, follow
[deploy/INSTALL.md](../deploy/INSTALL.md) for the install walk-through.
