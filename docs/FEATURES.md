# Features, in full

This page is the complete feature inventory. The README carries the six
features that decide whether QueryHub is the right shape for your problem. Read
this page when QueryHub is the right shape.

- **Two surfaces, one core** — the `/sql` modal in **Slack** and a **web UI**.
  In the modal, you pick the target and the database, paste SQL, and submit.
  The web UI is QueryHub Web: FastAPI + a static React bundle, with Slack-OIDC
  login. Both surfaces share the *same* submit → approve → execute → audit core.
  Approvals always arrive in Slack. Either surface serves results as CSV/XLSX
  with server-side paging.
- **Multiple engines** — **PostgreSQL** and **SQL Server** both have the full
  three-tier model. A pluggable `engines.py` spec classifies each statement and
  routes the driver (psycopg / pyodbc, including read-only routing for SQL
  Server AG). **Amazon Athena** and **ClickHouse** are read-only. ClickHouse uses
  the native protocol, readonly=1, and default-deny table functions. You add a
  new engine as a spec, not as scattered `if`s.
- **Read replicas, invisibly** — a read-only PostgreSQL query runs on a
  healthy read replica of its target, and otherwise on the primary. QueryHub
  measures the replica lag against the WAL position of the primary. If the
  replica fails the query, the query runs on the primary instead. People see one
  connection name. The result says that it came from a replica.
- Admin DM with **Approve / Reject / Request changes** buttons. One
  approval is enough. All admins see the resolution.
- **Batch submissions** — `/sql batch` (or the Single ↔ Batch radio
  toggle inside `/sql`) lets a user queue up to N items in one
  approval round. There are per-item buttons, plus the bulk actions
  "Approve / Reject all remaining". When every item in the bundle has a
  decision, a single summary DM arrives with every completed CSV.
- **Auto-approve grants** — per-user, time-bounded, tier-scoped
  exemption from admin approval. An exemption covers RO only, or tiers up to
  DDL. Matching queries skip the approval gate and dispatch immediately.
  Admins get a short FYI DM with the query inline. The admin panel offers these
  exemptions only where the person or team can already query. An access grant
  can carry one in the same step.
- **CSV bulk import** — with `/sql import`, a user with an import grant uploads
  a CSV. The bot `COPY`s it into the `dba` schema: into a new auto-created
  table (all TEXT) or into an existing `dba.*` table. An admin approves each
  import. The load runs with DDL credentials and with `synchronous_commit` off
  for speed. The schema is hard-pinned to `dba`, so the feature can never touch
  a prod schema. QueryHub purges uploaded CSVs after 24h.
- **Three-tier permission model**: RO / RW / DDL credentials per
  target. QueryHub classifies and audits each query.
- **Per-team and per-user grants** — `team_target_grants` and
  `user_target_grants` resolve the effective tier and allowed
  databases for each user × target.
- **Admin scopes** — per-admin `max_tier` + `scope_team_ids` +
  `scope_target_ids` narrow which requests each admin can approve.
- **Schema browser** — a *Browse schema* button in the `/sql` modal
  pushes a reference view. The view shows a table typeahead, columns (PK / NN /
  idx markers), indexes and FKs. You do not lose the draft query. The view
  reads an hourly bot-DB snapshot of the catalog of every target, with
  partitions collapsed into their parent. So browsing never touches a target.
  You can also use `/sql tables`, `/sql schema <table>` and fleet-wide
  `/sql findcol <pattern>`.
- **Inline EXPLAIN plans** — an `EXPLAIN` request returns its plan as
  a code block in the DM, not as a CSV file. A config toggle can enable
  `EXPLAIN ANALYZE` of read queries. Writes stay blocked, because ANALYZE
  executes the wrapped statement.
- **Scheduled execution** — submit now, and run at a chosen UTC time.
  You can cancel until execution starts. QueryHub re-evaluates auto-approve
  grants at the scheduled moment. So if a grant expires before the run, the
  request needs admin approval instead.
- **DDL escalation** — when the bot's role lacks ownership, the
  request transitions to `awaiting_dba_manual`. A human DBA can then
  finish it out-of-band and close it from Slack.
- **Encrypted at rest** — Fernet (symmetric) for target credentials,
  Slack tokens, and the bot DB password. The master key is a single
  file on disk. To migrate hosts, copy that one file.
- **Audit log** — QueryHub records every state change in the same
  transaction as the state change itself.
- **Product metrics** — `p_metrics_*` views in the bot DB: adoption,
  team usage, approval SLA, cost-savings estimates, ratings, usage
  overview with timeline annotations.
- **User ratings + feedback** — a post-completion DM prompt with a
  30-day cooldown, and optional free-text feedback for low ratings.
- **Slack-native access grants** — the `admins.can_grant` capability gates
  this feature (super-admins implicitly). `/sql grant` opens a modal to grant
  access: you pick the Slack user, one or more RDS targets, the tier
  (RO/RW/DDL) and an optional database restriction. You can also add
  auto-approve for their read-only queries, written with the grant. Granting
  also whitelists the user if needed (a grant is otherwise dormant), DMs them,
  and audits it. `/sql revoke` lists a user's grants and removes any. A granter
  cannot exceed their own tier ceiling or scope, and the bot's own control-plane
  DB is never grantable here.
- **Slash sub-commands** — `/sql help`, `/sql whoami`, `/sql history`,
  `/sql teams`, `/sql batch`, `/sql tables`, `/sql schema`,
  `/sql findcol`. Admin-only: `/sql grant`, `/sql revoke`, `/sql roles`,
  `/sql pending`, `/sql kill`. QueryHub generates the help list from the
  registry, so the list stays current automatically.
