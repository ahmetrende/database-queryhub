# Prerequisites

Have everything on this page ready **before** you follow
`deploy/INSTALL.md`. If you start from scratch, allow ~30 minutes for the
Slack-side setup and ~15 minutes for the DB-side setup.

> **No Slack? Skip section 1 entirely.** The **vanilla profile** runs
> QueryHub web-only:
>
> - base install (`pip install .`)
> - built-in local accounts for login (`scripts/create_local_user.py`)
> - approvals in the web admin panel
>
> Only sections 2–5 apply. You can add the Slack surface later. It is
> purely additive (`pip install '.[slack]'` + the section-1 app).

> **Surfaces & engines.** Section 1 covers the **Slack** surface. The
> **web UI** (QueryHub Web) is a separate process on the same core
> (`python -m queryhub.web`, FastAPI + TLS). With Slack installed, you
> can log in to it through Slack OIDC, and without Slack through local
> accounts. Target databases can be **PostgreSQL or SQL Server**. SQL
> Server targets also need the Microsoft ODBC driver (`msodbcsql18`) and the
> `mssql` extra (`pip install '.[mssql]'`).

## Contents

1. [Slack app](#1-slack-app)
2. [PostgreSQL — bot metadata DB](#2-postgresql--bot-metadata-db)
3. [PostgreSQL — target clusters](#3-postgresql--target-clusters)
4. [Linux host](#4-linux-host)
5. [Network](#5-network)
6. [Optional integrations](#6-optional-integrations)

---

## 1. Slack app

The bot runs as a custom Slack app in Socket Mode, so it needs no public
endpoint. Create the app once per workspace.

### Step-by-step

Create the app and its access:

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create
   New App** → **From scratch**. Give the app any name. The name becomes
   the display name in chat, e.g. `QueryHub`. Pick the target workspace.

2. **Socket Mode** → enable it. Click **Generate Token and Scopes**.
   Add the scope `connections:write`. Save the resulting **App-Level
   Token** (`xapp-…`). You will put this token in the bot's secrets.

3. **OAuth & Permissions** → **Scopes** → **Bot Token Scopes**: add these
   scopes.

   | Scope | Why |
   |---|---|
   | `chat:write` | Post DMs to admins + requesters |
   | `chat:write.customize` | Override `username` / `icon_emoji` on `chat.postMessage`, so that the bot appears under the configured display name |
   | `commands` | Register the `/sql` slash command |
   | `users:read` | Read the name / email / timezone for `profile_sync` |
   | `im:write` | Open DM channels with users when needed |
   | `files:write` | Upload result CSVs |

Configure the app, then install it:

4. **Slash Commands** → **Create New Command**:
   - Command: `/sql`
   - Short description: e.g. *"Submit a SQL query for admin approval"*
   - Usage hint: optional
   - Request URL: leave blank (Socket Mode handles it)

5. **Interactivity & Shortcuts** → **Interactivity**: ON.
   Request URL: leave blank.

6. **App Home** → **Show My Bot as Online**, if you want. Set the bot
   user's display name here. Slack uses this identity on file uploads,
   where `chat:write.customize` cannot override it. Recommended: match the
   bot's intended brand name.

7. **Install App** to your workspace. After the install, copy the **Bot
   User OAuth Token** (`xoxb-…`). You will put this token in the bot's
   secrets, next to the app-level token.

### What you walk away with

- `SLACK_BOT_TOKEN` (`xoxb-…`)
- `SLACK_APP_TOKEN` (`xapp-…`)
- Workspace admin install consent (one-time)

During install, `scripts/manage_env_secrets.py init` stores both tokens
encrypted in `/etc/queryhub/secrets.enc`.

---

## 2. PostgreSQL — bot metadata DB

The bot uses a single Postgres database for its own state: requests,
audit log, target registry, config, ratings, etc.

### Requirements

- PostgreSQL **14+**. The bot uses `pg_read_all_data` and
  `pg_write_all_data` (PG14 predefined roles), jsonb, partial indexes and
  CHECK constraints.
- The bot host must reach it over TCP on port 5432 (or your custom port).
- Roughly **2-5 GB** of disk to start. It grows with `audit_log` and
  `requests.explain_plan` (capped per row).
- The recommended setup is a dedicated database (e.g., `queryhub` on a
  shared Postgres host). The bot can also share a cluster with other apps,
  as long as it owns its own DB.

### One-time bootstrap (you'll run this during install)

To run `deploy/setup_db.sql` once, you need a Postgres admin user:
`postgres`, `rds_superuser`, or any role that can `CREATE DATABASE` +
`CREATE ROLE`. The script:

- Creates the `queryhub` login role (NOSUPERUSER, NOCREATEDB,
  NOCREATEROLE, connection-limit 20)
- Creates the `queryhub` database, owned by the `queryhub` role
- Grants nothing else: the bot is self-contained inside its own DB

After this, you no longer need the admin role.

---

## 3. PostgreSQL — target clusters

These are the Postgres servers that your developers actually want to
query.

### Requirements

- Postgres **14+** on every cluster.
- The bot host must reach each cluster on port 5432, with TLS. The bot
  uses `sslmode=require` by default. It can also check the server
  certificate per host (`target_ssl_verify_hosts`, see CONFIGURATION.md
  "Target TLS").
- You must be able to **create login roles** on each cluster. Otherwise,
  you need an admin who can run the bootstrap SQL once per target.

### Per-target setup

For each cluster that you want to expose through `/sql`, provision **at
least one** login role:

| Role | Required? | Privileges | Used for |
|---|---|---|---|
| `queryhub_ro` | **yes** | `pg_read_all_data` (PG14+) | RO queries (SELECT/EXPLAIN/etc.) |
| `queryhub_rw` | optional | `pg_read_all_data` + `pg_write_all_data` | RW queries (INSERT/UPDATE/DELETE/MERGE) |
| `queryhub_ddl` | optional | granular DDL grants (see below) or `rds_superuser` | DDL (CREATE/ALTER/DROP). The bot escalates owner-only ops to manual DBA execution, so you can keep this role narrow |

Helper script: `deploy/grant_readonly.sql` provisions `queryhub_ro`
in a single database. Run it once per database on each target.

The bot stores each role's password encrypted (Fernet) in
`target_servers.{password_encrypted, password_rw_encrypted,
password_ddl_encrypted}`. Per-target rotation is a one-liner UPDATE,
after you re-encrypt the password with `scripts/encrypt_secret.py`.

### DDL grants — the narrow path

If you want DDL execution but do not want to give `rds_superuser`, this is
the typical granular grant set:

```sql
CREATE ROLE queryhub_ddl WITH LOGIN PASSWORD '...'
    NOSUPERUSER NOCREATEDB NOREPLICATION CONNECTION LIMIT 5;

GRANT CONNECT ON DATABASE <db>     TO queryhub_ddl;
GRANT USAGE   ON SCHEMA public     TO queryhub_ddl;
GRANT CREATE  ON SCHEMA public     TO queryhub_ddl;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public TO queryhub_ddl;
GRANT USAGE   ON ALL SEQUENCES IN SCHEMA public TO queryhub_ddl;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON TABLES TO queryhub_ddl;
```

This set lets the role CREATE new objects and do all DML on existing ones.
It does **not** allow ALTER / DROP / CREATE INDEX on tables that the role
does not own. Such an operation moves the request to
`awaiting_dba_manual`. A human DBA then completes it out-of-band and
closes the request from Slack.

---

## 4. Linux host

The bot is a long-running daemon. A small VM or container is enough.

### Requirements

| | |
|---|---|
| OS | Linux with systemd. Ubuntu 22.04+ is tested. Any modern distro works |
| CPU / RAM | 1 vCPU / 512 MB is plenty for a small org. The need grows with request volume |
| Disk | ~2 GB for the venv + CSV results buffer (`/var/lib/queryhub/results`). An automatic cleanup deletes CSVs per `results_ttl_hours` |
| Python | `python3.11` + `python3.11-venv` + `python3-pip` |
| System packages | `libpq-dev`, `git` |
| Sudo | Needed during install to create `/etc/queryhub`, `/var/lib/queryhub`, `/var/log/queryhub` and to install the systemd unit |
| User | A normal Linux user (e.g. `ubuntu`) that owns the repo and the runtime directories. The service runs as this user, not as root |

### Filesystem layout (created during install)

```
/etc/queryhub/
├── env               # non-secret config (mode 600, owner $BOT_USER)
├── master.key        # Fernet master key (mode 600, owner $BOT_USER)
├── master.key.fingerprint
└── secrets.enc       # encrypted Slack tokens + bot DB password

/var/lib/queryhub/results/   # CSV results, auto-cleaned per TTL
/var/log/queryhub/           # not currently used; reserved for future
```

---

## 5. Network

| | |
|---|---|
| Outbound HTTPS to `slack.com` | Required for Socket Mode WebSocket + the Web API |
| Outbound TCP to bot metadata DB | Port 5432 (or your custom port) |
| Outbound TCP to every target DB | Port 5432 each. If you are on AWS with private subnets, the bot host's security group must reach the SG of each target |
| Inbound | **None.** Socket Mode means the bot has no public endpoint and accepts no incoming connections |
| DNS | Standard outbound DNS resolution |
| Time | NTP synced. Slack signs requests with timestamps, and a large drift breaks Socket Mode |

---

## 6. Optional integrations

These are nice-to-haves. The bot runs fine without them.

### Inventory view (for bulk target import)

`scripts/import_targets_from_inventory.py` and the hourly sync
wrapper read from a view, `inventory.v_all_databases(endpoint, database_name)`.
The view is in your bot DB (or in any DB that the bot can reach). If you
populate this view from your own inventory source, the bot auto-discovers
new endpoints and disables decommissioned ones. Without the view, you add
targets manually with the SQL templates in `deploy/db_admin_templates.sql`.

Suggested shape:

```sql
CREATE VIEW inventory.v_all_databases AS
SELECT endpoint, database_name FROM (your inventory source);
```

### Scheduled cleanup

`scripts/cleanup_old_results.py` deletes local CSV files and Slack
file uploads older than `results_ttl_hours` (default 72h). Schedule
it daily with one of these:

- a systemd timer (template in `deploy/INSTALL.md` section 12)
- a plain cron entry
- your existing job runner

### Log correlation

For audit and debugging, set `log_line_prefix` on each target RDS
parameter group to include the application name. Suggested value:

```
%m [%p] %q%u@%d %r %a %x %e %i
```

The bot sets `application_name=queryhub req=<id> by=<email-or-slack-id>`
on every target connection. With this prefix, the target's Postgres log
tells you which request ran a query, next to the query itself.

### Per-team Postgres role enforcement

For defense-in-depth beyond the bot's application-layer team grants,
provision a `queryhub_team_<name>` role on each target. Give it
team-scoped SELECT/INSERT/etc. privileges. Set
`team_target_grants.target_role` to the role name. The bot then runs
`SET LOCAL ROLE <role>` before the query, so Postgres enforces team
boundaries natively.

Helpers: `deploy/grant_team_role.sql`, and
`scripts/plan_team_role_provisioning.py`, which generates a runbook for
unprovisioned (team, target) pairs.

---

When all of the above is in place, follow
[deploy/INSTALL.md](../deploy/INSTALL.md) for the install walk-through.
