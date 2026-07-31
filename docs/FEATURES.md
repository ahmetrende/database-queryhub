# Features, in full

The README carries the six that decide whether QueryHub is the right shape for
your problem. This is the complete inventory, for when it is.

- **Two surfaces, one core** — the `/sql` modal in **Slack** (pick target,
  database, paste SQL, submit) and a **web UI** (QueryHub Web: FastAPI +
  static React bundle, Slack-OIDC login) that shares the *same*
  submit → approve → execute → audit core. Approvals always land in Slack;
  results serve from either surface as CSV/XLSX with server-side paging.
- **Multiple engines** — **PostgreSQL** and **SQL Server** (both full
  three-tier); a pluggable `engines.py` spec classifies each statement and
  routes the driver (psycopg / pyodbc, incl. SQL Server AG read-only
  routing). ClickHouse is defined read-only (spec). New engines are added as
  a spec, not scattered `if`s.
- Admin DM with **Approve / Reject / Request changes** buttons; one
  approval is enough; all admins see the resolution.
- **Batch submissions** — `/sql batch` (or the Single ↔ Batch radio
  toggle inside `/sql`) lets a user queue up to N items in one
  approval round. Per-item buttons + "Approve / Reject all remaining"
  bulk actions; a single summary DM lands with every completed CSV
  once the bundle is fully decided.
- **Auto-approve grants** — per-user, time-bounded, tier-scoped
  exemption from admin approval. RO-only or up to DDL; matching
  queries skip the approval gate and dispatch immediately. Admins
  get a short FYI DM with the query inline.
- **CSV bulk import** — `/sql import` (import-granted users) uploads a
  CSV and the bot `COPY`s it into the `dba` schema: a new auto-created
  table (all TEXT) or an existing `dba.*` table. Admin-approved; the
  load runs with DDL credentials, `synchronous_commit` off for speed,
  and the schema is hard-pinned to `dba` so the feature can never touch
  a prod schema. Uploaded CSVs are purged after 24h.
- **Three-tier permission model**: RO / RW / DDL credentials per
  target, classified per-query, audited.
- **Per-team and per-user grants** — `team_target_grants` and
  `user_target_grants` resolve the effective tier and allowed
  databases for each user × target.
- **Admin scopes** — per-admin `max_tier` + `scope_team_ids` +
  `scope_target_ids` narrow which requests each admin can approve.
- **Schema browser** — a *Browse schema* button in the `/sql` modal
  pushes a reference view: table typeahead + columns (PK / NN / idx
  markers), indexes and FKs, without losing the draft query. Fed from
  an hourly bot-DB snapshot of every target's catalog (partitions
  collapsed into their parent), so browsing never touches a target.
  Also `/sql tables`, `/sql schema <table>` and fleet-wide
  `/sql findcol <pattern>`.
- **Inline EXPLAIN plans** — an `EXPLAIN` request returns its plan as
  a code block in the DM instead of a CSV file. `EXPLAIN ANALYZE` of
  read queries can be enabled with a config toggle; writes stay
  blocked because ANALYZE executes the wrapped statement.
- **Scheduled execution** — submit now, run at a chosen UTC time;
  cancellable until execution starts. Auto-approve grants are
  re-evaluated at the scheduled moment so a grant that expires
  before the run falls back to admin approval.
- **DDL escalation** — when the bot's role lacks ownership, the
  request transitions to `awaiting_dba_manual` so a human DBA can
  finish it out-of-band and close it from Slack.
- **Encrypted at rest** — Fernet (symmetric) for target credentials,
  Slack tokens, and the bot DB password. The master key is a single
  file on disk; to migrate hosts, copy that one file.
- **Audit log** — every state change is captured in the same
  transaction as the state change itself.
- **Product metrics** — `p_metrics_*` views in the bot DB: adoption,
  team usage, approval SLA, cost-savings estimates, ratings, usage
  overview with timeline annotations.
- **User ratings + feedback** — post-completion DM prompt with a
  30-day cooldown, optional free-text feedback for low ratings.
- **Slack-native access grants** — `/sql grant` opens a modal to grant a
  Slack user access: pick the user, one or more RDS targets, the tier
  (RO/RW/DDL) and optional database restriction. Granting also
  whitelists the user if needed (a grant is otherwise dormant), DMs
  them, and audits it. `/sql revoke` lists a user's grants and removes
  any. Gated by the `admins.can_grant` capability (super-admins
  implicitly); a granter can't exceed their own tier ceiling or scope,
  and the bot's own control-plane DB is never grantable here.
- **Slash sub-commands** — `/sql help`, `/sql whoami`, `/sql history`,
  `/sql teams`, `/sql batch`, `/sql tables`, `/sql schema`,
  `/sql findcol`; admin-only `/sql grant`, `/sql revoke`, `/sql roles`,
  `/sql pending`, `/sql kill`. The help list is auto-generated from the
  registry, so it stays current automatically.
