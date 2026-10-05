<p align="center">
  <img src="QueryHubWeb/brand/svg/queryhub-avatar-green.svg" alt="QueryHub" width="128" height="128">
</p>

# QueryHub

**Per-query approval for production databases.** Developers never hold a
production credential. They submit a statement, and a DBA approves *that
statement*. QueryHub then executes it under a role that matches its
classification: RO, RW or DDL. It masks the result, delivers it, and audits
every decision. Apache-2.0, no enterprise tier.

<p align="center">
  <a href="../../actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/ahmetrende/database-queryhub/ci.yml?branch=main&label=CI" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/engines-PostgreSQL%20%7C%20SQL%20Server%20%7C%20Athena%20%7C%20ClickHouse-informational.svg" alt="Engines">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/changelog-keep--a--changelog-orange.svg" alt="Changelog"></a>
</p>

Two surfaces over one core: `/sql` in **Slack**, or the **web UI**. Engines:
**PostgreSQL**, **SQL Server**, **Amazon Athena** and **ClickHouse**. Athena and
ClickHouse are read-only.

<p align="center">
  <img src="docs/screenshots/hero.png" alt="A pending RW request in the approval queue: the SQL, its classification, the reason, and Approve / Reject / Request changes" width="900"><br>
  <em>The product in one screen: a developer's write is pending. The DBA sees the
  exact statement, its classification, what it will touch, and why the developer
  asked for it. Then the DBA approves that statement, not a session.</em>
</p>

- [What this is instead of](#how-this-compares) · [Screenshots](#screenshots)
  · [Try it](#try-it-in-two-minutes) · [Install](deploy/INSTALL.md)
  · [Architecture](docs/ARCHITECTURE.md) · [Operations](docs/OPERATIONS.md)
  · [Backup & recovery](docs/DISASTER_RECOVERY.md)
  · [Key rotation](docs/KEY_ROTATION.md)
  · [Personal data](docs/COMPLIANCE.md) · [Versioning](#versioning-and-support)
  · [Contributing](CONTRIBUTING.md)
  · [Changelog](CHANGELOG.md) · [Roadmap](ROADMAP.md)

> **Status: experimental / early.** This project runs in production for
> its author, but the public release is young. Interfaces, schema, and
> defaults may change, and hardening continues. Run it behind your own
> network controls. Do not expose it to the internet. See
> [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) for an honest
> inventory and [ROADMAP.md](ROADMAP.md) for what's next.

> **The problem this solves.** In a typical engineering org, the DBA team
> is the bottleneck for ad-hoc data questions. Examples: "why is this
> user missing?", "how many of X happened yesterday?", "I need a CSV of
> all Y for the report". Each question is a Slack DM or a ticket, and
> each one costs a context switch. The data path is informal and
> unaudited. Over time, access escalates as developers accumulate
> long-lived read-only roles "just for this one report".
>
> This bot is the **paved path**. Developers submit, and an admin
> approves. The bot executes against the right credential tier, and the
> results return as a CSV in a DM. Every step is in `audit_log`.
> Production access stays narrow, and ad-hoc work no longer blocks the
> DBA's calendar.

## How this compares

Two products solve adjacent problems. If you know either one, you should be
able to tell in twenty seconds whether this one is redundant.

| | QueryHub | Bytebase | Teleport |
| --- | --- | --- | --- |
| **What it governs** | one statement at a time | schema *changes* (migration review is the product) | *access to a session* — it brokers a connection and records the transcript |
| **Reads your SQL?** | yes — classifies each statement RO/RW/DDL and picks the credential to match | yes, for migration review | no — it cannot tell a SELECT from a DROP |
| **Developer holds a credential?** | never | for ad-hoc query, usually yes | yes — a short-lived one, but it is theirs |
| **Approve one query** | the whole model | secondary to change management | not a concept |
| **Where approval happens** | Slack DM or the web panel, and the result arrives as a CSV | web console | web console / CLI |
| **Cost of the approval workflow** | Apache-2.0, all of it | custom approval flows are Enterprise | access requests are Enterprise |

In short: **the alternatives charge for the two features that this project is
entirely about — per-query approval and a custom approval workflow.**

### Where you will lose by choosing this

Here are the losses, stated plainly. The wins above are only worth reading if
the losses are here too:

- **No schema-migration pipeline, no GitOps.** If reviewing and shipping
  migrations is your problem, Bytebase is built for it, and this project is not.
- **Four engines.** PostgreSQL, SQL Server, and Athena and ClickHouse (both read-only).
  Bytebase supports more than a dozen.
- **No HA.** One process, one host. See
  [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).
- **No infrastructure access.** Teleport governs SSH, Kubernetes and more.
  This project governs SQL statements and nothing else.
- **One maintainer**, against funded companies. The
  [status banner](#queryhub) is not false modesty.

### Why this shape is about to matter more

People give agents database credentials right now, because that is the easiest
way to let an agent answer a question. The problem is not new, and the fix is
not new either:

1. A caller that should not hold a credential submits a statement.
2. A human approves that statement.
3. The statement runs under a role that matches what it actually does.

QueryHub already does that, for humans.

Extending it to agents is a small addition, not a new product. The addition is a
principal kind for non-human callers, a hard read-only ceiling for them, and
their prompt recorded next to the SQL. It is on the roadmap
([P3, agent access](ROADMAP.md)), but it is **not built yet**. This section is
positioning, not a feature claim. Note what it is not: QueryHub would govern the
SQL that an agent writes. It would not write SQL from natural language.

### Not this project's job

QueryHub is not an ORM, a BI tool, or a general query IDE. It has no
dashboards, no charts, and no scheduled reports beyond "run this statement
later". If you want to explore data, use Metabase. QueryHub is the gate in front
of the database. It is not a tool to explore the data.

## Engines

An engine is in one of three states. Today, every engine that QueryHub names is
in the first state. The second state is the one that a new engine passes
through.

| Engine | State | What that means |
|---|---|---|
| **PostgreSQL** | **Executes** | Full three-tier model: RO / RW / DDL, per-tier credentials, streamed results, EXPLAIN pre-flight, PII lineage from the planner. It is the reference engine. |
| **SQL Server** | **Executes** | Same model. T-SQL safety dialect. QueryHub refuses cross-catalog and linked-server references. AG read-routing for RO. |
| **Amazon Athena** | **Executes, read-only** | For data that is now in object storage, not in the database. No host and no stored credential: the gateway assumes a role. QueryHub accepts only SELECT / WITH. On this engine, that rule is what refuses the statements that would WRITE (`CREATE TABLE AS`, `INSERT`, `UNLOAD`, `MSCK REPAIR`). The schema comes from the data catalog. Every query records what it scanned, because on a pay-per-byte engine the scan is the risk. |
| **ClickHouse** | **Executes, read-only** | Native protocol over TLS, as a login that the server holds at `readonly=1`. `readonly=1` refuses every setting, so the gateway sends none. The gateway enforces the time limit itself: it closes the connection at the deadline, which cancels the query. QueryHub accepts only SELECT / WITH. Table functions are default-deny (benign generators only). QueryHub refuses dictionary, remote and file reads. `system.*` and `information_schema` are readable. The schema catalog never wakes a service that idles to save compute. |
| *A new engine* | **Parses, refuses to run** | A safety spec, but no execution path yet. QueryHub rejects a target tagged with it **at execution time**, and does not silently run it through the PostgreSQL driver. |
| Anything else | **Not started** | No spec, no driver. Nothing to configure. |

That middle row is deliberate, not an unfinished corner. Connecting psycopg to a
non-PostgreSQL server would "work" often enough to be dangerous. It would also
skip the safety dialect of that engine. So a statement that PostgreSQL rules
classify as read-only could be a write under the rules of the target. QueryHub
refuses an engine that it can parse but cannot execute. This is the same
discipline as refusing SQL whose meaning is ambiguous: **if QueryHub cannot make
the guarantee, the query does not run.**

Adding an engine is a spec in [`engines.py`](src/queryhub/engines.py) —
sqlglot dialect, banned leading keywords, driver — plus a resolver in the
lineage and statement-count seams. It is not a fork.

## Screenshots

All of these are the shipped UI, rendered from the design prototype's mock data.
No real connection, person or query appears in any of them.

<p align="center">
  <img src="docs/screenshots/flow.gif" alt="Submit a query, see it auto-approved and masked, then approve a write as the DBA" width="820"><br>
  <em>The whole loop: you run a read-only query. QueryHub auto-approves it within
  your grant and masks PII in the result. Then you switch to the DBA side, review
  a pending write, and approve it. The audit log records the decision.</em>
</p>

<p align="center">
  <img src="docs/screenshots/web/welcome.png" alt="QueryHub web — workspace" width="820"><br>
  <em>The workspace: your connections by tier (RO/RW/DDL), recent history,
  saved queries, and open sessions — everything in one place.</em>
</p>

<p align="center">
  <img src="docs/screenshots/web/editor.png" alt="QueryHub web — SQL editor with masked results" width="820"><br>
  <em>You write SQL against a tier-matched connection and run it. QueryHub masks
  PII columns in the result and audits every run.</em>
</p>

<p align="center">
  <img src="docs/screenshots/web/grants.png" alt="QueryHub web — grants, per subject and connection" width="820"><br>
  <em>The three-tier model, made visible: who holds RO / RW / DDL, on which
  connection, scoped to which databases. A team grant and a user grant read the
  same way. The user grant wins.</em>
</p>

<p align="center">
  <img src="docs/screenshots/web/autoapprove.png" alt="QueryHub web — auto-approve grants, time-limited and row-capped" width="820"><br>
  <em>The controlled bypass: a trusted subject can skip review for reads that stay
  inside a row cap and an expiry. Both bounds are deliberate. An exemption with no
  time limit is a permanent grant with extra steps.</em>
</p>

<p align="center">
  <img src="docs/screenshots/web/audit.png" alt="QueryHub web — audit log" width="820"><br>
  <em>Every decision, attributed and recorded — approvals, rejections,
  auto-approvals, grant changes, and queries that the safety pass refused.</em>
</p>

<p align="center">
  <img src="docs/screenshots/web/editor-light.png" alt="QueryHub web — light theme, SQL Server schema browser" width="820"><br>
  <em>Light theme, with a SQL Server schema in the schema browser. QueryHub
  supports PostgreSQL and SQL Server behind one pluggable engine layer.</em>
</p>

<p align="center">
  <img src="docs/screenshots/slack/slack-modal.png" alt="The /sql modal in Slack: server, database, SQL, justification" width="470">
  <img src="docs/screenshots/slack/slack-approval.png" alt="The approval card in Slack: the statement, its tier, the reason, and Approve / Reject / Request changes" width="470"><br>
  <em>The same two moments in Slack, for teams that prefer not to open a web app.
  They are <code>/sql</code> to submit, and the card that a DBA acts on. The card
  shows the statement, its classification, and why the developer asked for it.
  When the DBA approves, the statement runs. The result returns as a file in a
  DM.</em>
</p>

<p align="center">
  <img src="docs/screenshots/web/login.png" alt="QueryHub web — sign in" width="440"><br>
  <em>Sign in with Slack, or with a local username/password in the vanilla
  (no-Slack) profile.</em>
</p>

## What it does

- **Per-statement approval.** The DBA approves the exact SQL that runs.
  QueryHub executes nothing on a credential that the developer could have used
  themselves.
- **Tier-matched execution.** QueryHub classifies every statement as RO / RW /
  DDL and runs it under the credential for that tier. It re-derives the tier at
  execution time, so an edited or superseded query cannot execute at a stale
  tier.
- **PII masked on output.** QueryHub masks values as they stream into the
  result file. It masks by content (IBAN, card, email, phone, national ids —
  checksum-validated, so false positives stay near zero). It also masks by
  column-name catalog, for free-text PII with no detectable shape. This is
  accidental-exposure mitigation, not a data boundary: the boundary is column
  privileges and RLS on the database itself.
  [COMPLIANCE.md](docs/COMPLIANCE.md#4-limiting-third-party-data-what-masking-does-and-does-not-do)
  says exactly where the limits are.
- **Two surfaces, one core.** `/sql` in Slack and the web UI drive the same
  submit → approve → execute → audit path. Approve wherever the interrupt
  arrives. The result returns as CSV/XLSX either way.
- **Auto-approve, deliberately narrow.** Read-only requests can run instantly
  under a per-user, time-bounded, tier- and target-scoped grant. Everything
  else waits for a human.
- **An audit row for every state change**, written in the same transaction as
  the change. So there is no "it happened but nobody logged it" state.

**[docs/FEATURES.md](docs/FEATURES.md)** has the full feature list. It covers
batch submission, CSV bulk import, scheduled execution, schema browser, inline
EXPLAIN plans and DDL escalation. It also covers admin scopes, per-team grants,
product metrics, ratings, and the full slash-command list.


## Try it in two minutes

```bash
git clone https://github.com/ahmetrende/database-queryhub.git
cd database-queryhub
docker compose up
```

Then open **http://localhost:8080** in a browser. Sign in as `demo-admin` /
`queryhub-demo`, or as `demo-dev` for the developer's view.

You get three containers: the app, its metadata database, and a throwaway
"production" Postgres. That Postgres holds a seeded shop schema (10k users, 24k
orders, 40k events) plus the three per-tier login roles. The demo developer
holds an RO grant. Try this:

1. Submit `SELECT id, email, phone FROM users LIMIT 10;`.
2. Approve it as `demo-admin`.
3. Watch the result arrive with the email and phone masked.
4. Try an `UPDATE`. Watch QueryHub refuse it, because it exceeds the grant.

> **Demo only.** It has fixed passwords, a generated master key with no custody
> plan, and plain HTTP on localhost. It also relaxes `target_ssl_mode` to
> `prefer`, because the demo target has no TLS. Use it only to try QueryHub. Do
> not run anything real on it. The install below is for real use.

## Prerequisites

Before you install, you need:

| | |
|---|---|
| **(Optional) Slack workspace** | Only for the Slack surface (`/sql` + Slack approvals/DMs). You need admin access to create / install a custom Slack app with Socket Mode + Bot Token + App Token. Without it, QueryHub runs **web-only** with built-in local accounts — the *vanilla profile* (see below) |
| **PostgreSQL — bot metadata DB** | A Postgres instance that can give the bot one dedicated DB (`queryhub` by default). Hosting agnostic: RDS, self-managed, or local for dev |
| **At least one target DB** | The cluster(s) that developers query — **PostgreSQL or SQL Server**. Per-tier login roles: `queryhub_ro` / `_rw` / `_ddl` on Postgres, matching logins on SQL Server |
| **Linux host** | A small VM or container that can run Python 3.11+ continuously and reach Slack + your DBs. The docs describe the install path for systemd. A different supervisor works fine |
| **Python 3.11+** | Plus `python3.11-venv`, `libpq-dev`, `git` |
| **(Optional) Web UI** | To expose the web surface, run `python -m queryhub.web` (FastAPI/uvicorn) behind TLS. Serve the `QueryHubWeb/` bundle. Web login is either **Slack OIDC** (a Slack app's client id/secret) or **built-in local accounts** (username/password, no Slack) |
| **(Optional) SQL Server driver** | For SQL Server targets: Microsoft ODBC driver (`msodbcsql18`) + the `mssql` extra — `pip install '.[mssql]'` (pulls `pyodbc`) |
| **(Optional) ClickHouse driver** | For ClickHouse targets: the `clickhouse` extra — `pip install '.[clickhouse]'` (pulls `clickhouse-driver`). Native protocol over TLS, port 9440 |
| **A Postgres superuser (or rds_superuser) for bootstrap** | `deploy/setup_db.sql` uses it **once**, to create the bot's metadata DB and login role |

[docs/PREREQUISITES.md](docs/PREREQUISITES.md) has the full pre-install
checklist: Slack app scopes, per-target role provisioning, network rules, and
optional integrations.

## Install

Run two commands. Then do everything else in the UI.

```bash
cp .env.example .env && $EDITOR .env     # metadata DB + first admin
docker compose -f docker-compose.install.yml up -d
```

The first `up` **builds the image from this checkout**. That takes a few
minutes, once. This default is deliberate: an approval gateway should not change
what it runs because a shared tag moved. To skip the build, pull a released
image. Then name it in the `up` command:

```bash
docker pull ghcr.io/ahmetrende/database-queryhub:1.0.34
QH_IMAGE=ghcr.io/ahmetrende/database-queryhub:1.0.34 docker compose -f docker-compose.install.yml up -d
```

Every release publishes its own immutable tag alongside `:latest`. Reference the
version, not `latest`, in anything that you deploy.

Four values in `.env` matter. `BOT_DB_HOST` / `BOT_DB_PASSWORD` are for the
metadata database. `QH_ADMIN_USER` names the account that the first start
creates. If you leave `QH_ADMIN_PASSWORD` empty, the first start generates a
password and prints it once in `docker compose logs app`. Either way, it is a
bootstrap password. It is good for a single login, and then QueryHub asks you
to set a real one.

The first start generates the master key, waits for the database, applies
the migrations and creates that admin. On a restart, QueryHub never touches any
of it again.

From there, nothing needs a terminal:

1. Sign in.
2. **Add a connection** (alias, host, database, the three per-tier passwords).
3. **Test** it.
4. **Create a team**.
5. **Grant** the team the tier that it should have.

The one thing that the UI cannot do for you is to create the login roles on the
database that you expose. That is a `GRANT` on their cluster, not ours.
[deploy/NEW_TARGET.md](deploy/NEW_TARGET.md) describes these scripts step by
step: [deploy/grant_readonly.sql](deploy/grant_readonly.sql),
`grant_readwrite.sql`, `grant_ddl.sql` and `grant_mssql.sql`.

This install deliberately leaves two things to you:

- **TLS**. The port binds to loopback. Put a reverse proxy in front of it. Set
  `WEB_BASE_URL` + `web_cookie_secure=on`.
- **Backups of the metadata database**, which holds the audit log.

### On a host, without Docker

The guided installer does the same setup on a plain VM (venv → keys → DB →
migrations → first admin → TLS → systemd). It is idempotent, and it prompts for
secrets instead of reading them from a file:

```bash
bash scripts/install.sh
```

Full walk-through, including how to run the Slack surface next to the web
app: [deploy/INSTALL.md](deploy/INSTALL.md). Day-to-day operations:
[docs/OPERATIONS.md](docs/OPERATIONS.md).

### With or without Slack

The base install runs **web-only**: no Slack workspace, no Slack SDK. The
`/sql` bot, Slack approvals and Slack DMs are all in the optional `[slack]`
extra. Without that extra, those calls do nothing, and the web UI is the whole
product. Approvals happen in the web admin panel. Results and notifications
appear in the app.

Login then uses **built-in local accounts** instead of Slack SSO. The admin
above is one of these accounts. The first-start bootstrap of the container
creates only the first one. To add more from a shell, run the script below. It
reads the password interactively and stores it only as a salted PBKDF2 hash,
never in cleartext:

```bash
python scripts/create_local_user.py --username alice --admin
```

The script also sets `web_auth_local_enabled` to on. Enabling the Slack
surface later is purely additive (`pip install '.[slack]'` + set the tokens).
Local and Slack accounts are distinct principals, and they can coexist.

## Architecture

- **Shared core, two surfaces** — a transport-agnostic
  submit → decide → execute core (`core_submit.py`, `core_decide.py`,
  `executor.py`). Two surfaces drive it: the **Slack** app (Bolt + Socket
  Mode, no public endpoint) and **QueryHub Web** (FastAPI,
  `python -m queryhub.web`). Approvals happen in Slack *or* in the web
  admin panel, with the same decision core either way. See
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Pluggable engines** — `engines.py` carries one spec per engine (sqlglot
  dialect, read-only flag, keyword classification, driver). The executor
  dispatches on that spec. PostgreSQL and SQL Server run every tier. ClickHouse and Amazon Athena run read-only. Adding an engine
  means adding a spec.
- **Postgres metadata DB** — stores config, the target server registry,
  admins, requests, the audit log, and per-DM message refs. With these refs,
  all admin DMs update together when one admin acts.
- **Three-tier permission model** — RO / RW / DDL credentials per
  target. Queries connect with the creds that match their classified tier.
- **Background executor** — `ThreadPoolExecutor` runs approved
  queries with `statement_timeout` and streams rows into a capped CSV.
- **Encrypted at rest** — Fernet (symmetric) ciphertext for target
  credentials, Slack tokens, and the bot DB password. The master key is a
  single file (`/etc/queryhub/master.key`), and it is the only on-disk
  secret. To migrate hosts, copy that one file.

Full adapter/port model: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Every `bot_config` knob: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).
Web session/auth design: [docs/AUTH.md](docs/AUTH.md).

```
src/queryhub/
├── main.py             # entry point: Slack Bolt + Socket Mode
├── config.py           # env vars + bot_config table reads
├── db.py               # metadata DB pool + db.transaction() helper
├── crypto.py           # Fernet wrapper around master.key
├── errors.py           # libpq / ODBC error scrubbing for user messages
├── engines.py          # per-engine spec: dialect, safety, driver (pg/mssql/clickhouse)
├── query_safety.py     # static SQL safety (leading-keyword allow-list)
├── ast_safety.py       # sqlglot AST second pass
├── pre_flight.py       # EXPLAIN at submit + risk hints
├── pii.py              # result PII masking (content + column-name layers)
├── core_submit.py      # transport-agnostic submit pipeline (Slack + web share it)
├── core_decide.py      # transport-agnostic approve / reject / request-changes
├── executor.py         # runs approved queries, builds CSV/XLSX, delivers
├── mssql_exec.py       # SQL Server execution (pyodbc + AG read-only routing)
├── athena_exec.py      # Athena execution (assumed role, bytes scanned in the audit)
├── clickhouse_exec.py  # ClickHouse execution (native protocol, own deadline watchdog)
├── replicas.py         # where an RO request runs: a healthy read replica or the primary
├── query_secrets.py    # a submitted password is stored masked, kept encrypted only while it can run
├── csv_import.py       # /sql import (COPY into the dba schema)
├── targets.py          # target_servers CRUD + per-tier credentials
├── target_policy.py    # alias/host allow-deny globs for target sync
├── teams.py            # team / grant resolution (effective_grant_for_user)
├── grants.py           # grant / revoke cores (+ grantee DM)
├── admins.py           # admin list + scope check + temp-admin grants
├── auto_approve.py     # time-bounded, tier+target-scoped approval exemptions
├── requesters.py       # allowlist + bypass · row_limits.py · ratings.py
├── schema_catalog.py   # hourly target-schema snapshot + browse/search
├── auth_events.py      # authorization-change outbox → DM poller
├── bundles.py · templates.py · favorites.py   # batch, saved queries, favorites
├── audit.py            # audit_log writes (transactional, atomic with state)
├── slack_app/          # Slack surface — modal, notifications, subcommands,
│                       #   ro_window, schema_browser, admin_grant, handlers
└── web/                # QueryHub Web — FastAPI app + Slack-OIDC auth +
                        #   sessions + routes_* + mapping (static UI in QueryHubWeb/)
```

## Slack app required scopes

- **Bot Token**: `chat:write`, `commands`, `users:read`, `im:write`,
  `files:write`
- **App Token** (Socket Mode): `connections:write`
- **Slash command**: `/sql`
- **Interactivity**: enabled (Socket Mode needs no Request URL)

## Operations

All admin tasks are raw SQL against the bot DB or short CLI scripts.
There are no long-running admin CLIs. See
[docs/OPERATIONS.md](docs/OPERATIONS.md) for copy-paste snippets.
Quick links:

- **Bot lifecycle** — `systemctl restart queryhub`,
  `journalctl -u queryhub -f`
- **Encrypted secrets** —
  `scripts/manage_env_secrets.py {init,list,set,remove}`
- **Targets** — `scripts/encrypt_secret.py`, then an INSERT from
  `deploy/db_admin_templates.sql`
- **Admins / requesters / teams / grants** — INSERTs / UPDATEs in
  `deploy/db_admin_templates.sql` and `deploy/team_admin_templates.sql`
- **Runtime config** — `UPDATE bot_config SET value=... WHERE key=...`

## Audit & history

```sql
SELECT id, requester_slack_id, status, decided_by_slack_id,
       row_count, created_at, completed_at
  FROM requests ORDER BY id DESC LIMIT 50;

SELECT * FROM audit_log WHERE request_id = 42 ORDER BY created_at;
```

`docs/OPERATIONS.md` section 10 has more patterns (in-flight, by user, plan
capture, etc.).

## Security model

> Found a vulnerability? Report it **privately**, not in a public issue. See
> [SECURITY.md](SECURITY.md) (GitHub **Security → Report a vulnerability**).

- **Application layer**: requesters allowlist → team / user grants →
  query-safety analysis → admin approval → tier-matched credentials at
  execute time. The query-safety analysis has an allow-list of leading
  keywords, a multi-statement reject, a CTE-DML reject, WHERE-required for
  UPDATE/DELETE, and always-true-WHERE detection.
- **Postgres layer (optional)**: a per-team `target_role`, granted at the
  cluster level. The executor `SET LOCAL ROLE`s into it before it runs
  the query. See `deploy/grant_team_role.sql` and
  `scripts/plan_team_role_provisioning.py`.
- **Crypto layer**: QueryHub encrypts target credentials and the env secrets
  file with Fernet and a single master key (mode 0600 on disk).
- **PII masking**: QueryHub masks result values as they stream into the
  CSV/XLSX file. It records which kinds fired in `audit_log` and shows them
  to the requester. Toggle: `bot_config.pii_masking_enabled`. Two layers:
  1. **content-based** — by value, not column name, so an aliased / wrapped
     column cannot hide PII from the mask. It covers email, phone, Turkish
     national ID (TCKN), tax number (VKN), IBAN and card number. The last
     four are checksum-validated (TC/VKN algorithms, IBAN mod-97, Luhn +
     network prefix) to keep false positives near zero.
  2. **column-name catalog** (`pii_column_patterns`) for free-text PII with
     no detectable format (name, address). QueryHub masks a result column by
     type when its name matches a configured pattern. Operators extend the
     catalog with no code change.
- **Audit**: every state change has a paired `audit_log` insert in the same
  transaction (`db.transaction()` + `audit.log_in()`).

## Versioning and support

QueryHub uses SemVer. This section says exactly what the major number covers.
It does not leave that to the word "stable".

**Inside the compatibility promise** — a minor release will not break these:

- **`bot_config` keys** documented in [docs/CONFIGURATION.md](docs/CONFIGURATION.md).
  A key may gain a value. It will not change meaning or disappear.
- **the audit contract**, below.
- **migrations are append-only**, below.

**Outside it, and deliberately so:**

- **The HTTP endpoints the bundled UI calls.** They are that UI's private
  interface, not an integration surface. The screen that consumes them shapes
  them, and both ship together. Building against them means pinning a
  version. If you want a supported programmatic surface, open an issue. Saying
  this plainly is better than a stability promise that nobody keeps.
- **The frontend**, entirely.
- **The database schema.** Migrations are forward-only, and
  `apply_migrations.py` handles the upgrade. But a downgrade is a restore. So
  treat the schema as ours, and the audit contract as yours.

**Will not change without a major version:**

- **the audit contract** — every state change keeps a row, written in the same
  transaction as the change. No release makes an existing `audit_log` row
  unreadable or ambiguous.
- **migrations are append-only** — nobody edits a released migration. A later
  migration supersedes it instead. The ledger stores a checksum, so that an
  edit is caught.
- **the tier model** — a statement classified RO never executes on a
  write-capable credential.

**Support window.** Security fixes land on `main` and on the most recent tagged
release. There is no LTS and no backporting to older minors. One maintainer
([MAINTAINERS.md](MAINTAINERS.md)) cannot honestly promise one, so the project
does not promise one. If you pin a version, plan to upgrade for security fixes.

Release process, including what is validated before a tag:
[RELEASING.md](RELEASING.md).

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the development setup (including the frontend build), and see
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Record changes worth a line in the
release notes in [CHANGELOG.md](CHANGELOG.md).

## License

Licensed under the [Apache License 2.0](LICENSE) —
`SPDX-License-Identifier: Apache-2.0`. There is no open-core split: every
feature described above is in this repository.

Dependencies carry their own terms, and one of them matters:
**psycopg is LGPL-3.0**. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists
that licence and every other third-party licence. It also covers the engine
trademarks and the proprietary ODBC driver from Microsoft that the SQL Server
extra needs.
