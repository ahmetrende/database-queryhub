# Bot metadata DB — schema reference

This file describes every persistent object the bot reads/writes in its
metadata database (the `slackbot` DB on the configured Postgres host). Schema is
applied automatically via `scripts/apply_migrations.py`; descriptions here
are also stored as `COMMENT ON ...` in the DB itself, so DataGrip / psql
`\d+ <object>` shows the same prose.

Connection: `slackbot` DB on the configured Postgres host
(e.g. `<your-host>.<region>.rds.amazonaws.com`), owner `slackbot` (LOGIN,
NOSUPERUSER, NOCREATEDB, NOCREATEROLE, connection limit 20).

---

## Migration history

| File | What it does |
|------|--------------|
| `001_initial_schema.sql` | bot_config, target_servers, admins, requests, request_notifications, audit_log + `request_status` enum |
| `002_seed_config.sql` | seed bot_config defaults (max_rows, csv_size_mb, query_timeout_sec, ...) |
| `003_teams.sql` | teams, team_members, team_target_grants + v_team_summary, v_user_targets |
| `004_slack_file_id.sql` | requests.slack_file_id (for CSV cleanup) |
| `005_access_requests.sql` | access_requests, access_request_notifications |
| `006_bot_display_config.sql` | seed bot_display_name + bot_display_icon in bot_config |
| `007_requesters_and_email.sql` | requesters table; admins.email; bot_config.{log_level, results_ttl_hours} |
| `008_descriptions.sql` | COMMENT ON for every object (this file's prose, in the DB) |
| `009_team_target_role.sql` | team_target_grants.target_role for SET LOCAL ROLE |
| `010_scheduled_status.sql` | request_status enum gains `scheduled` |
| `011_request_scheduling.sql` | requests.scheduled_for + bot_config.max_schedule_days + DM tracking cols |
| `012_user_timezone.sql` | admins.tz + requesters.tz for local-time DMs |
| `013_requester_bypass.sql` | requesters.bypass_team_grants flag |
| `014_rw_ddl_grants.sql` | three-tier model: team_target_grants.mode + user_target_grants table + target_servers.{username_rw,password_rw_encrypted,username_ddl,password_ddl_encrypted} |
| `015_kill_switch.sql` | bot_config.kill_switch + kill_switch_message |
| `016_pre_flight_explain.sql` | bot_config.pre_flight_explain + query_plan_logging + requests.explain_plan jsonb |
| `017_rate_limit.sql` | bot_config.max_open_requests_per_user |
| `018_set_allowlist.sql` | bot_config.set_allowed_params (SET LOCAL prelude allowlist) |
| `019_request_ratings.sql` | request_ratings table + bot_config.rating_enabled + v_rating_* views (later renamed to p_metrics_*) |
| `020_product_metrics.sql` | p_metrics_* namespace, p_metrics_cost_savings + p_metrics_volume_{daily,weekly,monthly} + p_metrics_team_usage + p_metrics_top_users + p_metrics_scheduled_usage + p_metrics_tier_distribution + p_metrics_failure_breakdown + p_metrics_admin_workload; bot_config.cost_* knobs; p_metrics_cfg_num helper |
| `021_more_metrics.sql` | p_metrics_target_heatmap + p_metrics_peak_hours + p_metrics_business_vs_offhours + p_metrics_approval_sla; bot_config.report_timezone; p_metrics_cfg_text helper |
| `022_dba_manual_escalation.sql` | request_status enum gains `awaiting_dba_manual` (DDL escalation path) |
| `023_admin_scopes.sql` | admins.{max_tier, scope_team_ids, scope_target_ids} — per-admin approval scoping (NULL = no restriction) |
| `024_who_can_what_view.sql` | `p_metrics_who_can_what` — one row per active user (admin / bypass / team grants / user grants), powers `/sql roles` |
| `025_usage_view.sql` | `metric_annotations` table (timeline markers) + `p_metrics_usage_daily` overview view with annotations joined per day |
| `026_request_bundles.sql` | `request_bundles` table + `bundle_status` enum + `requests.{bundle_id, position}` + `request_notifications.bundle_id`; `bot_config.{batch_enabled, batch_max_items}` feature flag |
| `027_bundle_status_trigger.sql` | AFTER UPDATE trigger on `requests.status` keeps `request_bundles.status` in sync automatically (pending / partial / decided / cancelled rollup) |
| `028_bundle_summary_dm.sql` | `request_bundles.requester_summary_{channel_id, message_ts}` — idempotency anchors for the bundle-summary DM sent to the requester |
| `029_fix_bundle_trigger_and_nullable.sql` | `request_notifications.request_id` → NULL'able (paired with the bundle_id CHECK); trigger gains `pg_advisory_xact_lock(bundle_id)` to serialise concurrent recomputes |
| `030_auto_approve.sql` | `auto_approve_grants` table (per-user, time-bounded, tier-scoped exemption from admin approval) + `v_active_auto_approve` view |
| `031_temp_admin_grants.sql` | `temp_admin_grants` table (time-bounded admin role for vacation / on-call coverage) + `v_active_temp_admins` view. Only super-admins (permanent admin with all scope columns NULL) can issue. `admins.is_admin` / `can_approve` / `list_active` consult both tables. |
| `032_report_excluded_users.sql` | `report_excluded_users` table + three wrapper views (`requests_reportable`, `audit_log_reportable`, `request_ratings_reportable`) that the `p_metrics_*` views read from. Lets operators hide their own self-test traffic from reports without touching authz / audit paths. |
| `033_audit_reportable_fix.sql` | Fix `audit_log_reportable` (and `request_ratings_reportable`) to filter by the linked request instead of the action's actor. An excluded user's approvals of OTHER people's real requests now stay in admin reports. |
| `034_daily_views_gap_fill.sql` | Gap-fill the daily-bucket reporting views (`p_metrics_volume_daily`, `p_metrics_usage_daily`) via `generate_series`. Days with zero traffic now appear as 0 rows instead of being absent — keeps the x-axis dense so weekend bands and other date-driven overlays line up. |
| `035_report_start_date.sql` | `bot_config.report_start_date` knob (default `2026-05-01`) + every time-axis `p_metrics_*` view now filters `created_at >= report_start_date`. Replaces the rolling 90-day window on the daily / peak-hours / scheduled-usage views. |
| `036_ast_safety_flag.sql` | `bot_config.ast_safety_enabled` — toggles the sqlglot AST second-pass safety check (`ast_safety.py`). |
| `037_result_format.sql` | `requests.result_format` (`'csv'` default) — per-request CSV vs XLSX output choice. |
| `038_query_templates.sql` | `query_templates` table (saved/shared `/sql` queries) + owner / shared indexes. |
| `039_request_facts.sql` | `p_metrics_request_facts` view — one denormalized row per reportable request; the dashboard's single data source (inlined as JSON, client-side aggregated). |
| `040_risk_hints.sql` | `requests.risk_summary` (admin-DM risk line) + `bot_config.{risk_seq_scan_rows, risk_high_cost}` thresholds for the pre-flight plan analysis. |
| `041_pii_masking.sql` | `bot_config.pii_masking_enabled` — toggles content-based PII masking (`pii.py`) in result output. |
| `042_submission_failures.sql` | `submission_failures` table — forensic log of rejected modal submissions (validation errors), admin-only. |
| `043_pii_column_patterns.sql` | `pii_column_patterns` table — column-NAME catalog (token/substring/regex → pii_type) for masking free-text PII (name/address) the content scan can't detect. |
| `044_query_fingerprint_cache.sql` | `requests.query_fingerprint` + partial index + `bot_config.{fingerprint_cache_enabled, fingerprint_cache_ttl_days}` — RO-only repeat-query auto-approve cache. |
| `045_pii_patterns_mobile.sql` | Seed `pii_column_patterns` with phone tokens (`mobile`, `tel`, `phone`). Idempotent (`ON CONFLICT DO NOTHING`). |
| `046_csv_import.sql` | CSV bulk-import: `import_grants` (per-user allowlist), `csv_imports` (one row per import), `import_notifications` (admin DM anchors) + `bot_config.{csv_import_enabled, import_max_rows, import_max_mb, import_csv_ttl_hours, import_timeout_sec}`. |
| `047_csv_import_column_defs.sql` | `csv_imports.column_defs` JSONB — user-supplied typed schema (`[{name,type}]`, allow-list validated) for new-table imports; NULL = all-TEXT. |
| `048_metrics_csv_imports.sql` | `p_metrics_csv_imports` view — one denormalized row per CSV import joined to the target alias, minus rows whose requester is in `report_excluded_users`. Feeds the dashboard's CSV-import section. |
| `049_query_favorites.sql` | `query_favorites` table — per-user starred queries (personal, no sharing). Populated from the result-DM ⭐ button or the /sql modal's "favorite this" checkbox; surfaced in a modal favorites picker. Deduped per (user, query, target, db). |
| `050_pii_masking_exemptions.sql` | `pii_masking_exemptions` table — scoped opt-outs from PII masking for public-data scopes (e.g. OpenSanctions). NULL columns are wildcards: target-wide / db-wide / table / column. Table-level rows apply only when the query references ONLY exempt tables (sqlglot; fail-closed on joins / unparseable SQL). Runtime-effective. |
| `051_auto_approve_target_scope.sql` | `auto_approve_grants.{target_server_id, database_name}` (nullable; NULL = legacy broad). Lets a grant be narrowed to one target/db; `effective_grant` matches via the pure `auto_approve.grant_covers()` predicate. `v_active_auto_approve` widened (DROP+CREATE so it stays re-runnable). |
| `052_auto_approve_requests.sql` | `auto_approve_requests` table — user-requested short RO auto-approve windows (RO-burst nudge): mandatory justification, admin-decided; on approval inserts a target-scoped `auto_approve_grants` row. `bot_config` keys `ro_burst_threshold` / `ro_burst_window_min` / `ro_window_minutes` seeded. |
| `053_pii_exemption_apply_in_joins.sql` | `pii_masking_exemptions.apply_in_joins` (bool, default FALSE). When TRUE, a table+column-scoped exemption also fires in JOINs (its table just has to be among the query's tables) instead of only on single-table queries — opt-in, accepts a same-named co-joined column being unmasked too. For columns non-sensitive db-wide (e.g. event titles). |
| `054_user_grant_revoked_at.sql` | `user_target_grants.revoked_at` (TIMESTAMPTZ, NULL = active). Soft-disable for the stale-grant reaper (`scripts/reap_stale_grants.py`): revokes a per-user grant when the user hasn't queried the target in `grant_idle_revoke_days`. Every read of `user_target_grants` (teams.py + `v_effective_user_grants`) filters `revoked_at IS NULL`. New `bot_config`: `grant_reaper_enabled` (off), `grant_idle_revoke_days` (30). |

---

## Tables

### `bot_config`

Runtime knobs as a key/value table. The bot reads on every relevant request,
so changing a value via `UPDATE bot_config SET value = ... WHERE key = ...`
takes effect immediately — no restart needed (with the exception of
`log_level`, which is read once at process start).

Current keys:

| Key | Default | Description |
|-----|---------|-------------|
| `max_rows` | `1000` | Max rows returned in CSV result. |
| `csv_size_mb` | `10` | Hard cap on CSV file size before the bot refuses to upload. |
| `query_timeout_sec` | `300` | Per-query Postgres `statement_timeout`. |
| `require_justification` | `false` | If true, modal forces the requester to fill in a Justification. |
| `min_query_length` | `6` | Reject queries shorter than this many chars (sanity check). |
| `bot_display_name` | `QueryHub` | Username override for chat.postMessage (requires `chat:write.customize`). |
| `bot_display_icon` | `:query_hub:` | Icon emoji override for chat.postMessage. Workspace needs a custom emoji uploaded with this shortcode. |
| `log_level` | `INFO` | Python logging level. **Read at startup** — restart to apply. |
| `results_ttl_hours` | `72` | How long Slack file uploads + local CSV results persist before cleanup deletes them. |
| `max_schedule_days` | `7` | Max days into the future a /sql query can be scheduled. 0 = scheduling disabled. |
| `kill_switch` | `off` | When `on`, the bot rejects new `/sql` invocations and shows `kill_switch_message`. In-flight requests are unaffected. |
| `kill_switch_message` | (text) | Message shown to users when the kill switch is on. |
| `pre_flight_explain` | `on` | When `on`, the bot runs `EXPLAIN` with RO credentials before approval to surface plan-time errors and (when `query_plan_logging=on`) capture the plan. RO queries only. |
| `query_plan_logging` | `off` | When `on`, the EXPLAIN (FORMAT JSON) output is stored in `requests.explain_plan` (jsonb). |
| `max_open_requests_per_user` | `5` | Per-user concurrency cap. Counts non-terminal requests; new submissions over the cap are rejected. |
| `set_allowed_params` | (csv list) | Comma-separated allowlist of GUC names the user is allowed to set via a `SET LOCAL` prelude (e.g. `work_mem,statement_timeout`). Everything else is rejected. |
| `rating_enabled` | `on` | Send a 1-5 rating DM after terminal-state requests (suppressed for 30 days per user). |
| `cost_dba_minutes_per_request` | `8` | Used by `p_metrics_cost_savings` — avg DBA time saved per self-service request. |
| `cost_dba_hourly_usd` | `75` | Used by `p_metrics_cost_savings` — fully-loaded DBA hourly rate. |
| `cost_avoided_replicas` | `5` | Used by `p_metrics_cost_savings` — read replicas avoided by routing queries through the bot. |
| `cost_per_replica_monthly_usd` | `200` | Used by `p_metrics_cost_savings` — monthly cost per replica. |
| `cost_other_monthly_usd` | `0` | Used by `p_metrics_cost_savings` — catch-all monthly savings line. |
| `report_timezone` | `UTC` | IANA tz used by time-bucketed `p_metrics_*` views (peak hours, business-vs-offhours). |
| `batch_enabled` | `off` | When `on`, users can run `/sql batch` and see a Single ↔ Batch radio toggle at the top of the `/sql` modal. When `off`, the sub-command is hidden and the toggle isn't rendered. |
| `batch_max_items` | `5` | Maximum items per `/sql batch` submission. Slack modal view caps blocks at 100; ~5 items keeps the modal readable. |
| `report_start_date` | `2026-05-01` | Lower bound (inclusive) for every time-axis `p_metrics_*` view. Days / weeks before this date are excluded. |

### `target_servers`

The Postgres servers the bot can query. One row per (host, default DB,
user) tuple. The bot connects with the per-row credentials; passwords are
Fernet-encrypted with the master key on disk and stored as ciphertext in
`password_encrypted`.

| Column | Notes |
|--------|-------|
| `id` | SERIAL, referenced by `team_target_grants.target_server_id`. |
| `alias` | Unique, shown in the `/sql` modal dropdown. |
| `host`, `port`, `default_database`, `username` | Connection coords. `username` is the RO login (typically `dba_slackbot_ro`). |
| `password_encrypted` | Fernet ciphertext of the RO user's password. Generate via `scripts/encrypt_secret.py` then INSERT raw. |
| `username_rw`, `password_rw_encrypted` | RW login (typically `dba_slackbot_rw` with `pg_read_all_data` + `pg_write_all_data`). Used when the effective tier for (user, target) is `rw` or `ddl` and the query classifies as write. NULL = RW not configured; write queries on this target are rejected. |
| `username_ddl`, `password_ddl_encrypted` | DDL login (typically `dba_slackbot_ddl`). Used when the effective tier is `ddl` and the query classifies as DDL. NULL = DDL not configured; DDL on this target is rejected before execution. |
| `enabled` | Soft-delete flag. Disabled targets disappear from the modal (admins still see them). |
| `notes` | Free text — describe purpose, owner, on-call team, etc. |
| `created_at`, `updated_at` | Timestamps. |

### `admins`

Slack users authorized to approve/reject `/sql` requests. Admins also
**bypass** the requester allowlist (`requesters`) and team grants
(`team_target_grants`) — they can submit `/sql` against any target.

Approval scope is configurable per-admin. All three scope columns NULL
= approve everything. Set any of them to restrict.

| Column | Notes |
|--------|-------|
| `slack_user_id` | PK, the `U…`/`W…` ID from Slack. |
| `name` | Display name (free text). |
| `email` | Lazily backfilled by the bot from Slack `users.info`. |
| `tz` | IANA timezone, lazily filled from `users.info`. Used to render DM timestamps in the admin's local time. |
| `enabled` | Soft-delete. |
| `max_tier` | NULL / `ro` / `rw` / `ddl`. Highest tier this admin can approve. `rw` covers ro+rw but not ddl. NULL = any tier. |
| `scope_team_ids` | `INTEGER[]` of `teams.id`. NULL = any team. Non-NULL = admin only sees / approves requests from a requester who belongs to at least one of these teams. A requester with no team membership is **not** matched by a non-NULL scope. |
| `scope_target_ids` | `INTEGER[]` of `target_servers.id`. NULL = any target. Non-NULL = admin only sees / approves requests whose target is in this list. |
| `added_at`, `added_by` | Audit. |

The scope check lives in `admins.can_approve(slack_id, request)` and is
consulted both at notification fan-out (to decide which admins see
buttons on a given request) and at action handling (defense in depth —
a button click is re-validated server-side).

### `requesters`

Allowlist of Slack users who may invoke `/sql`. The first authorization
layer (kill-switch). Behavior:

- Empty table (no enabled rows) ⇒ **bot is open to all** workspace users.
- Any enabled rows ⇒ only those listed users may run `/sql`. Everyone else
  gets the "no access" ephemeral and never reaches the modal.
- Admins always pass regardless of this table.

| Column | Notes |
|--------|-------|
| `slack_user_id` | PK, validated by CHECK (`^[UW][A-Z0-9]{8,}$`). |
| `email` | Lazily backfilled by the bot from Slack `users.info`. |
| `name` | Display name (free text). |
| `tz` | IANA timezone, lazily filled from `users.info`. Used to render scheduling / completion DMs in the user's local time. |
| `enabled` | Disable to revoke without losing audit history. |
| `bypass_team_grants` | When true, the user bypasses team/user grants entirely and reaches every enabled target (still subject to the requester allowlist). Use sparingly — typically for on-call DBAs who shouldn't sit inside a team. |
| `added_at`, `added_by` | Audit. |

### `teams`

Logical grouping of users. A team is granted access to one or more targets
(via `team_target_grants`); members of the team inherit those grants.

| Column | Notes |
|--------|-------|
| `id` | SERIAL. |
| `name` | Unique (e.g. `payment`, `ingest`). |
| `description` | Free text. |
| `created_at` | Audit. |

### `team_members`

Many-to-many: which Slack users belong to which teams.

| Column | Notes |
|--------|-------|
| `team_id` | FK to `teams(id)`, ON DELETE CASCADE. |
| `slack_user_id` | Validated by CHECK pattern. |
| `added_at` | Audit. |

PK is `(team_id, slack_user_id)`. A user can be in multiple teams.

### `team_target_grants`

Which targets (and which databases on each target) a team can reach, and
optionally which Postgres role on the target to impersonate.

| Column | Notes |
|--------|-------|
| `team_id` | FK to `teams`, ON DELETE CASCADE. |
| `target_server_id` | FK to `target_servers`, ON DELETE CASCADE. |
| `allowed_databases` | `text[]` — `NULL`/empty array = all DBs allowed; non-empty = only those DBs (bot-side check). |
| `mode` | `ro` (default), `rw`, or `ddl`. The tier this grant authorises. The effective tier for (user, target) is the most permissive of the user's team grants (ro < rw < ddl), unless a `user_target_grants` row overrides it. |
| `target_role` | Optional Postgres role name on the target. When set, the bot does `SET LOCAL ROLE <target_role>` inside the query transaction, so Postgres enforces the team's privileges natively. NULL = run as the bot's login user (`target_servers.username` / `username_rw` / `username_ddl`). Provision the role on the target with `deploy/grant_team_role.sql`. |
| `granted_at` | Audit. |

PK is `(team_id, target_server_id)`. A team can have grants on multiple
targets; for a given target, the array narrows down which DBs and
`target_role` narrows down which permissions on that target.

#### `target_role` flow

1. **Once per (team, target cluster):** DBA runs `deploy/grant_team_role.sql`
   on the target with `role_name=slackbot_team_<X>` and `bot_login=<bot's
   target user>`. Then GRANTs the schema/table privileges that role should
   have.
2. **Once per (team, target):** DBA UPDATEs `team_target_grants.target_role`
   to that role name (template in `deploy/team_admin_templates.sql`).
3. **Every query:** executor `SET LOCAL ROLE <target_role>` before
   `cur.execute(user_query)`. Auto-resets at COMMIT.

### `user_target_grants`

Per-user overrides on top of team grants. If a row exists here for
`(slack_user_id, target_server_id)`, it **entirely supersedes** any team
grants the user might have on that target — both `allowed_databases` and
`mode`.

Use it to:
- give a user access to a target their team doesn't have
- restrict a user to `ro` on a target where the team has `rw`
- elevate a single user to `ddl` without elevating the whole team

| Column | Notes |
|--------|-------|
| `slack_user_id` | Validated by CHECK pattern. |
| `target_server_id` | FK to `target_servers`, ON DELETE CASCADE. |
| `allowed_databases` | `text[]`. NULL / empty = all DBs on the target. |
| `mode` | `ro` / `rw` / `ddl`. |
| `granted_at`, `granted_by` | Audit. |

PK is `(slack_user_id, target_server_id)`. The view
`v_effective_user_grants` shows the resolved row the bot will use at
runtime (user override vs aggregated team grants).

### `requests`

Every `/sql` submission, regardless of outcome. The audit trail.

| Column | Notes |
|--------|-------|
| `id` | BIGSERIAL, shown to users as `#42`. |
| `requester_slack_id`, `requester_name` | Who submitted. |
| `target_server_id`, `database_name`, `query` | What was submitted. |
| `wants_result`, `justification` | Modal inputs. |
| `status` | enum `request_status` (see below). |
| `decided_by_slack_id`, `decided_by_name`, `decision_reason`, `decided_at` | Approver / rejector details. |
| `executed_at`, `completed_at` | Execution timing. |
| `row_count`, `truncated` | Result stats. |
| `error_message` | Filled when status=`failed`. |
| `csv_file_path` | Local CSV path (under `/var/lib/slackbot/results/`). NULL'd by cleanup. |
| `slack_file_id` | Slack file ID from `files_upload_v2`. NULL'd by cleanup after `files.delete`. |
| `scheduled_for` | When the user wants the query to run. NULL = run immediately on approval. If set, approval moves status to `scheduled` and the bot's scheduler thread picks it up at the right time. Capped by `bot_config.max_schedule_days`. |
| `requester_dm_channel_id`, `requester_dm_message_ts` | Coordinates of the user's "approved + scheduled" DM that carries the [Cancel] button. Used by `chat.update` when the request is cancelled or starts executing. NULL for non-scheduled requests. |
| `bundle_id`, `position` | Set when this row is one item of a `/sql batch` submission (FK to `request_bundles`, ON DELETE SET NULL; `position` is 1-based ordering inside the bundle). NULL for legacy single-shot submissions. |
| `result_format` | `'csv'` (default) or `'xlsx'` — output file format chosen in the modal (migration 037). |
| `explain_plan` | `jsonb` — captured EXPLAIN (FORMAT JSON) plan when `query_plan_logging` is on; capped at 64 KB (migration 016). |
| `risk_summary` | One-line admin-DM risk hint derived from the pre-flight plan (size band + seq-scan flag). NULL for non-explainable / fail-open (migration 040). |
| `query_fingerprint` | Literal-normalized sqlglot fingerprint of the query, set for RO submissions. Powers the RO repeat-query auto-approve cache; a partial index serves the lookup (migration 044). |
| `created_at` | Submission time. |

When `decided_by_slack_id = 'AUTO'`, the request was short-circuited by an `auto_approve_grants` row OR an RO fingerprint-cache hit instead of an admin button — `decided_by_name` carries the grant id + tier + expiry, or the matched prior request id, for audit.

### `request_notifications`

Tracks every admin DM the bot has posted for a given request OR bundle,
so when one admin clicks Approve/Reject the bot can `chat.update` all
of them in lockstep (so the buttons disappear from every admin's DM,
not just the deciding one).

| Column | Notes |
|--------|-------|
| `request_id` | FK to `requests(id)`, ON DELETE CASCADE. NULL when this row tracks a per-bundle DM (mutually exclusive with `bundle_id`). |
| `bundle_id`  | FK to `request_bundles(id)`, ON DELETE CASCADE. Set when this row tracks the per-(bundle, admin) DM that carries one block per item. Mutually exclusive with `request_id`. |
| `admin_slack_id` | Which admin received the DM. |
| `channel_id`, `message_ts` | Coordinates for `chat.update`. |

CHECK constraint enforces exactly one of `request_id` / `bundle_id` is
set. Two partial UNIQUE indexes — one per `(request_id, admin)` for
single-shot rows, one per `(bundle_id, admin)` for bundle rows.

### `audit_log`

Append-only log of state transitions and notable events on requests.
Holds free-form `details` (JSONB) for whichever transition wrote the
row. Current action labels:

| Action | Written when |
|--------|--------------|
| `submitted` | Requester submits the modal. |
| `approved` | Admin clicks Approve (immediate or scheduled). |
| `rejected` | Admin clicks Reject. |
| `changes_requested` | Admin sends back for edits. |
| `cancelled` | Requester or admin cancels a scheduled request before it runs. |
| `execution_started` | Executor opens the target connection and is about to run. |
| `completed` | Executor finishes (with or without a result set). |
| `failed` | Executor errors out. |
| `escalated_to_dba` | DDL hit `InsufficientPrivilege`; status flipped to `awaiting_dba_manual`. |
| `completed_manually` | Admin clicks [Mark completed] on an `awaiting_dba_manual` request. |
| `failed_manually` | Admin clicks [Mark failed] on an `awaiting_dba_manual` request. |
| `auto_approved` | An `auto_approve_grants` row short-circuited the admin gate; `actor_slack_id` = `'AUTO'`; `details` carries `grant_id` + `max_tier`. Sibling `submitted` row carries `auto_approved: true` in its details for symmetry. |

| Column | Notes |
|--------|-------|
| `request_id` | FK to `requests(id)`, ON DELETE SET NULL (so audit survives a request delete). |
| `actor_slack_id`, `actor_name` | Who took the action. NULL for system-driven steps (executor). |
| `action` | Short verb. |
| `details` | JSONB — extra context (row_count, error, etc.). |
| `created_at` | When. |

### `access_requests`

A user without team grants can submit one of these to ask for access. Body
includes the target/database/query they want plus a free-text reason.
Admins approve or reject from a DM; the actual team-membership / grant
INSERT is done by the admin in their IDE (the bot does not auto-grant).

Per `(user, target_server_id, attempted_query)` only one **pending** row
allowed (unique partial index on `md5(query)`). Once decided, a fresh
pending can be re-created for the same combo.

| Column | Notes |
|--------|-------|
| `id` | BIGSERIAL. |
| `requester_slack_id`, `requester_name` | Who submitted. |
| `target_server_id` | FK to `target_servers`, ON DELETE SET NULL. |
| `database_name`, `attempted_query` | Optional context. |
| `reason` | Required free text. |
| `status` | `'pending'`, `'approved'`, `'rejected'` (CHECK constrained). |
| `decided_by_slack_id`, `decided_by_name`, `decision_reason`, `decided_at` | Decision details. |
| `created_at` | Submission time. |

### `access_request_notifications`

Same lockstep-update mechanism as `request_notifications`, but for
`access_requests`. Tracks each admin DM so chat.update can replace the
button block on every admin's copy when any admin decides.

| Column | Notes |
|--------|-------|
| `access_request_id` | FK to `access_requests(id)`, ON DELETE CASCADE. |
| `admin_slack_id`, `channel_id`, `message_ts` | DM coordinates. |

UNIQUE(`access_request_id, admin_slack_id`).

### `request_ratings`

User-supplied 1-5 rating + optional free-text feedback for a single
request. Captured via a follow-up DM after the request reaches a
terminal state (`completed` / `failed` / `rejected` / `cancelled`).
One rating per request (UNIQUE on `request_id`). The bot suppresses
the prompt for 30 days after a user's most recent rating so survey
fatigue stays bounded; the whole thing toggles off via
`bot_config.rating_enabled = 'off'`.

| Column | Notes |
|--------|-------|
| `id` | SERIAL. |
| `request_id` | FK to `requests(id)`, UNIQUE, ON DELETE CASCADE. |
| `slack_user_id` | Who rated. |
| `rating` | `SMALLINT` 1-5 (CHECK). |
| `feedback_text` | Optional free text. |
| `rated_at` | Timestamp. |

Feeds the `p_metrics_rating_*` views (weekly avg, response rate, low
rating drill-down).

### `metric_annotations`

Free-form milestones overlaid on the product-metric dashboards. One
row per event (go-live, access cutover, incident, config change). The
`p_metrics_usage_daily` view joins these by day, so a daily chart can
label its bars.

| Column | Notes |
|--------|-------|
| `id` | SERIAL. |
| `occurred_at` | TIMESTAMPTZ — exact moment of the event. |
| `label` | Short marker text (under ~40 chars). Shown on the chart. |
| `description` | Optional longer note — context, owner, runbook / postmortem link. |
| `created_at` | When the annotation row was inserted. |

UNIQUE(`occurred_at, label`) so re-running migration seeds is idempotent.

### `request_bundles`

Parent row of a `/sql batch` submission. Each item lives in `requests`
with `bundle_id` pointing here. Single-shot `/sql` submissions don't
create a row here (`requests.bundle_id IS NULL`).

| Column | Notes |
|--------|-------|
| `id` | BIGSERIAL, shown to users as `B#42`. |
| `requester_slack_id`, `requester_name` | Who submitted. |
| `justification` | Bundle-level (single field across all items). |
| `scheduled_for` | NULL = run immediately on approval; otherwise the bundle's items move to `scheduled` and the bot's scheduler thread picks them up at that time. |
| `status` | `bundle_status` enum. Driven by an AFTER UPDATE trigger on `requests.status` — see migration 027 / 029. |
| `requester_summary_channel_id`, `requester_summary_message_ts` | Idempotency anchor for the bundle-summary DM the requester gets when every item is decided. First fire creates the message; subsequent state changes (e.g. a manual DBA closure hours later) `chat.update` the same DM. |
| `created_at` | Submission time. |

Bundle status rules (computed by the trigger):

- any item in `pending / approved / scheduled / executing / awaiting_dba_manual / changes_requested` → `pending`
- all items `cancelled` → `cancelled`
- mix of `completed` + at least one terminal-negative (`rejected / failed / cancelled`) → `partial`
- otherwise (all completed, all rejected, all failed) → `decided`

### `auto_approve_grants`

Per-user, time-bounded, tier-scoped exemption from the admin approval
gate. A query whose `required_mode` is ≤ `grant.max_tier`, submitted
while `NOW()` falls inside `[starts_at, expires_at)`, skips the
pending → approval step and dispatches directly. Higher-tier queries
fall back to the normal admin flow with a user-facing warning.

| Column | Notes |
|--------|-------|
| `id` | BIGSERIAL, referenced from `requests.decided_by_name` for audit. |
| `slack_user_id` | Validated by CHECK pattern. Multiple grants per user are allowed (most-permissive wins). |
| `max_tier` | `ro` / `rw` / `ddl`. Highest tier this grant covers (`rw` covers ro+rw, `ddl` covers everything). |
| `target_server_id` | NULL = grant covers every target (legacy/broad). Non-NULL = only auto-approves requests against this target. Added in migration 051; powers the narrow per-target RO windows. |
| `database_name` | NULL = any database on the (scoped) target. Non-NULL = only this database. Ignored when `target_server_id` IS NULL. Migration 051. |
| `starts_at` | Defaults to NOW(). |
| `expires_at` | NULL = no expiry. CHECK constraint: if both bounds are set, `expires_at > starts_at`. |
| `reason` | Free text — why this grant was issued. |
| `granted_by`, `granted_at` | Audit. |

Scheduling interaction: when a request has `scheduled_for` in the
future, the bot also evaluates the grant at that moment — a grant
that's valid now but expires before the scheduled run time falls back
to admin approval (so we don't auto-approve a query that nobody is
allowed to run by the time it executes).

### `auto_approve_requests`

User-requested short RO auto-approve windows — the **RO-burst nudge**.
When a user has run ≥ `ro_burst_threshold` RO queries in the last
`ro_burst_window_min` minutes, the `/sql` modal banner offers a
`ro_window_minutes` read-only auto-approve window scoped to the target
they hit most. The request carries a mandatory justification and is
fanned out to admins with Approve / Reject. On approval the bot inserts
a target-scoped `auto_approve_grants` row (`max_tier='ro'`,
`expires_at = NOW() + window`) and links it back via `granted_id`. If the
user already holds an active grant, the banner only nudges toward Batch.

| Column | Notes |
|--------|-------|
| `id` | BIGSERIAL. Carried in the admin DM button values + audit details. |
| `requester_slack_id` | Validated by CHECK pattern. |
| `target_server_id`, `database_name` | Scope of the requested window (db NULL = all dbs on the target). |
| `max_tier` | `ro` today (the nudge only offers RO). |
| `window_minutes` | Window length; the granted row expires `NOW() + this`. |
| `reason` | NOT NULL — mandatory justification. |
| `status` | `pending` / `approved` / `rejected`. Partial unique index allows one `pending` per (user, target). |
| `decided_by_slack_id`, `decided_by_name`, `decided_at` | Admin decision audit. |
| `granted_id` | FK → `auto_approve_grants.id` created on approval (NULL until then). |

Audit actions: `auto_approve_window_approved` / `auto_approve_window_rejected`.

### `temp_admin_grants`

Time-bounded admin role for vacation / on-call coverage. Only a
**super-admin** (a permanent `admins` row with `max_tier`,
`scope_team_ids`, and `scope_target_ids` ALL NULL and `enabled = TRUE`)
can issue these — enforced in the Python helper
`admins.grant_temp_admin()` and documented for the raw-SQL path in
`docs/OPERATIONS.md` §21.

The deputy is treated as an admin for the entire `[starts_at,
expires_at)` window. `admins.is_admin` / `can_approve` /
`list_active` consult `temp_admin_grants` alongside the permanent
`admins` table — a user with any matching scope row in either table
passes.

| Column | Notes |
|--------|-------|
| `id` | SERIAL. Referenced in `admins.NotASuperAdmin` exceptions and audit. |
| `slack_user_id` | The deputy. Validated by CHECK pattern. Multiple grants per user are allowed (most-permissive wins). |
| `max_tier` | NULL = wildcard. `ro` / `rw` / `ddl` narrow as on `admins.max_tier`. |
| `scope_team_ids` | INT[]. NULL = any team. Same semantics as `admins.scope_team_ids`. |
| `scope_target_ids` | INT[]. NULL = any target. Same semantics as `admins.scope_target_ids`. |
| `starts_at` | Defaults to NOW(). |
| `expires_at` | NULL = no auto-expiry (revoke manually). CHECK: if both bounds set, `expires_at > starts_at`. |
| `reason` | Free text — vacation, schema migration window, on-call shift, etc. |
| `granted_by` | The super-admin who issued the grant. |
| `granted_at` | When the row was inserted. |
| `revoked_at` | Set to NOW() to expire a grant early. The row stays in place for audit; `v_active_temp_admins` filters revoked rows out. |

Permanent `admins` rows are never deleted by this feature — the
table stays immutable so "who was admin on date Y" stays
answerable forever.

### `query_templates`

Saved `/sql` queries a user can reload into the modal (migration 038).

| Column | Notes |
|--------|-------|
| `id` | BIGSERIAL. |
| `name`, `description` | Label + optional blurb. `name` 1–64 chars (CHECK). |
| `query`, `target_server_id`, `database_name` | The saved payload. `target_server_id` FK ON DELETE SET NULL (template survives a target removal). |
| `owner_slack_id` | Owner. CHECK pattern `^[UW][A-Z0-9]{8,}$`. |
| `is_shared` | TRUE = visible to the whole workspace; FALSE = owner-only. Two partial indexes (owner / shared) back the listing. |
| `created_at`, `updated_at`, `last_used_at`, `use_count` | Lifecycle + usage stats. |

### `query_favorites`

Per-user starred queries (migration 049) — a lighter, personal-only sibling
of `query_templates`: no name, no sharing. Populated from the result-DM ⭐
button or the /sql modal's "favorite this" checkbox; surfaced in a modal
favorites picker (same prefill mechanism as templates/history).

| Column | Notes |
|--------|-------|
| `id` | BIGSERIAL. |
| `slack_user_id` | Owner. CHECK pattern `^[UW][A-Z0-9]{8,}$`. |
| `query`, `target_server_id`, `database_name` | The starred payload. `target_server_id` FK ON DELETE SET NULL. |
| `label` | Optional; the picker falls back to a query preview. |
| `created_at`, `last_used_at`, `use_count` | Lifecycle + usage stats. |

A unique index on `(slack_user_id, md5(query), COALESCE(target_server_id,-1),
COALESCE(database_name,''))` dedupes re-stars into a `last_used_at` touch;
`favorites.add()` trims each user back to `MAX_PER_USER` (50), least-recently-used
dropped.

### `submission_failures`

Forensic log of modal submissions the bot rejected at validation (migration
042). Append-only, admin-only — **not** read by any `p_metrics_*` view or the
dashboard. Indexed by `(slack_user_id, created_at DESC)`.

| Column | Notes |
|--------|-------|
| `id`, `created_at` | PK + when. |
| `slack_user_id`, `slack_user_name` | Who hit the wall. |
| `mode` | `'single'` or `'batch'`. |
| `target_server_id`, `database_name`, `query` | Best-effort snapshot of the rejected attempt (NULL when unparseable). |
| `errors` | `jsonb` — `{block_id: message, ...}`, the field-level errors returned to the modal. |

### `pii_column_patterns`

Column-NAME catalog for PII masking (migration 043) — the only way to mask
free-text PII (name / address) the content scanner can't detect by value. A
result column whose name matches a row here is masked by `pii_type` WITHOUT a
content re-check. Read once per result set in `pii.column_pii_map()`.

| Column | Notes |
|--------|-------|
| `id` | BIGSERIAL. |
| `pattern` | Lowercase token / substring / regex to match against the column name. |
| `pii_type` | `email｜phone｜tckn｜vkn｜iban｜card｜name｜address｜generic` — selects the masker. |
| `match_type` | `token` (split name on `_ - space`, exact token), `substring`, or `regex`. Default `token`. |
| `enabled` | Soft on/off. |
| `notes`, `created_at` | Audit. UNIQUE `(pattern, match_type)`. |

> **Known limitation:** name/address masking is column-name-based only. A query
> that aliases or wraps such a column (`SELECT full_name AS x`) bypasses it —
> there is no content detector for names. Content-detectable PII
> (email/phone/TCKN/VKN/IBAN/card) is **not** bypassable this way: the content
> layer scans every non-catalog cell. Keep name/address columns out of reach via
> the grant model, not masking.

### `pii_masking_exemptions`

Scoped opt-outs from PII masking (migration 050) — for data that is public
record (e.g. an OpenSanctions mirror), where masking person names is
technically correct but business-wise wrong. NULL scope columns are wildcards;
the four usable shapes:

| Scope | Row shape | Effect |
|-------|-----------|--------|
| target | `(target, NULL, NULL, NULL)` | all masking off for the whole target |
| database | `(target, db, NULL, NULL)` | all masking off for that database |
| table | `(target, db, table, NULL)` | masking off only when the query references **only** exempt tables — a join with any non-exempt table keeps masking ON (sqlglot table extraction, CTE aliases excluded; unparseable SQL = no exemption, fail-closed) |
| column | `(target, db, table-or-NULL, column)` | the named result column passes through unmasked (catalog + content scan both skipped); a table-scoped column row additionally requires the only-that-table condition |

Resolved per statement in `pii.exemption_decision()` — runtime-effective, no
restart. Every exempted execution writes a `pii_masking_exempted` audit row and
the requester DM carries an `:unlock:` note, so the forensic trail survives.
`enabled` is a soft off-switch; UNIQUE on the COALESCEd scope quadruple keeps
re-inserts idempotent.

### `import_grants`

Per-user CSV-import allowlist (migration 046). Admins bypass this table
entirely (same as the RW/DDL grant path) — a row is only needed for a
non-admin importer. `slack_user_id` is the PK; `granted_by` / `granted_at` /
`reason` are audit.

### `csv_imports`

One row per `/sql import` submission — the import-side analogue of `requests`
(migration 046; `column_defs` added in 047). The target schema is **always**
`dba` (the `table_name` column is unqualified and the schema is pinned in code).

| Column | Notes |
|--------|-------|
| `id` | BIGSERIAL, shown as `#42`. |
| `requester_slack_id`, `requester_name` | Who submitted. |
| `target_server_id`, `database_name`, `table_name` | Destination. `table_name` is a normalized single identifier; schema is hard-pinned to `dba`. |
| `is_new_table` | TRUE = CREATE then COPY; FALSE = COPY into an existing `dba.*` table. |
| `unlogged` | New-table only: create UNLOGGED (default TRUE) for load speed; user can pick permanent. |
| `delimiter` | `,` / `;` / tab. |
| `columns` | `jsonb` — normalized CSV header (the COPY column list when `column_defs` is NULL). |
| `column_defs` | `jsonb` `[{name,type}]` — user-supplied typed schema for a new table; types are allow-list validated. NULL = all-TEXT (migration 047). |
| `row_count`, `byte_size` | Parsed CSV stats. `inserted_rows` = rows actually COPYed. |
| `csv_file_path`, `slack_file_id` | Local copy + Slack file id; purged after `import_csv_ttl_hours`. |
| `status` | CHECK `pending｜approved｜executing｜completed｜failed｜rejected`. |
| `decided_by_slack_id`, `decided_by_name`, `decided_at`, `decision_reason` | Approver details. |
| `error_message` | Filled on failure. |
| `requester_dm_channel_id`, `requester_dm_message_ts` | Requester DM anchors. |
| `created_at`, `executed_at`, `completed_at` | Timing. |

### `import_notifications`

Per-(import, admin) admin-DM anchors — the import-side analogue of
`request_notifications`, so an approve/reject `chat.update`s every admin's copy
in lockstep. `import_id` FK ON DELETE CASCADE; `(admin_slack_id, channel_id,
message_ts)` carry the `chat.update` coordinates.

---

## Web surface, identity and schema cache

These arrived with the web UI, local accounts and the schema browser, and were
missing from this document.

### `web_sessions`

One row per sign-in (migration 061). Holds the hashed refresh token, the
previous hash (single-use rotation with reuse detection, migration 062), the
principal, the auth provider and `expires_at` / `revoked_at`. Access tokens are
short-lived and stateless; this table is what makes revocation immediate.
Expired and revoked rows are purged by the retention job
(`auth_session_retention_days`, default 7).

### `web_saved_sessions`

Named workspaces a user chose to sync server-side (migration 064) — open tabs
and their SQL. Distinct from `web_sessions`: user content, not auth. Purged
after `web_session_retention_days` (default 30) without a touch.

### `web_notification_reads`

Per-(principal, notification) read markers for the in-app feed, so the bell's
unread state survives a reload and follows the user across devices.

### `local_users`

Username/password accounts for the vanilla profile (migration 075). Passwords
are PBKDF2-HMAC-SHA256, salted and versioned — never reversible.
`must_change_pw` forces a reset before the account can run anything; `enabled`
is checked on every request, so disabling locks the account out at once. These
identities appear elsewhere as `local:<username>`, a namespace disjoint from
Slack ids.

### `auth_event_outbox`

Transactional outbox for authorization changes (migration 060). Triggers on the
authorization tables append a row inside the same transaction as the
grant/revoke, and a poller turns rows into DMs — so a change made by ANY path,
including direct SQL, still notifies the affected user. `processed_at` marks
completion; `attempts` / `last_error` bound retries. The Slack process runs the
poller, and so does the web process in the vanilla profile; processed rows are
trimmed after `auth_outbox_retention_days` (default 14).

### `schema_tables` / `schema_columns`

Hourly snapshot of every reachable target schema, backing `/sql tables`,
`/sql schema`, `/sql findcol` and the web schema browser. A cache, not a source
of truth: it is rewritten wholesale by the sync, so it can lag a DDL change by
up to an hour.

### `user_row_limit_overrides`

Time-bounded per-user raises of the row/size caps (migration 059), so a one-off
large export does not require changing the fleet default. Expired rows simply
stop applying.

### `target_pod_owner`

Maps each target to its owning team and lead (migration 070), populated from an
external service catalog. Used for routing and reporting; absence is not an
error.

### `mssql_host_map`

Per-target SQL Server node map used for read routing: which host serves reads
for an Availability Group, so RO queries can reach a readable secondary without
depending on the listener's redirect.

### `schema_migrations`

The migration ledger: one row per applied file with its sha256 checksum, so a
re-run is a no-op and a file edited after being applied is detected instead of
silently diverging.

---

## Views

Two namespaces:

- `v_*` — operational / debugging views (team summaries, user grants).
- `p_metrics_*` — product KPIs. Read-only aggregations; safe to expose
  to anyone with read access on the bot DB. Numeric / text config
  values are pulled from `bot_config` via the helpers
  `p_metrics_cfg_num(key, default)` and `p_metrics_cfg_text(key, default)`
  so the views stay live without redeploys.

### Operational

| View | What it shows |
|------|---------------|
| `v_team_summary` | Per-team: id, name, description, member count, grant count, created_at. |
| `v_user_targets` | For every (Slack user, target) pair the user can reach via team membership + grant: alias, host, default database, per-team `allowed_databases`. |
| `v_effective_user_grants` | Resolved (user, target, mode, allowed_databases) the bot uses at runtime. `source` column tells you whether the row came from a `user_target_grants` override or aggregated team grants. |
| `v_active_auto_approve` | One row per currently-active auto-approve grant (`NOW()` inside `[starts_at, expires_at)`). Multiple rows per user possible; readers should pick the highest `max_tier` when summarising. |
| `v_active_temp_admins` | One row per currently-active temp admin grant. Filters out rows where `revoked_at` is set. Backs `admins.is_admin` / `can_approve` / `list_active` extensions. |
| `requests_reportable` / `audit_log_reportable` / `request_ratings_reportable` | Filtered wrappers around the base tables that drop rows touched by `report_excluded_users`. The `p_metrics_*` views read from these instead of the raw base tables, so excluding a user from reports is a single INSERT. |

### Product metrics

| View | What it shows |
|------|---------------|
| `p_metrics_cost_savings` | Rolling cost-savings estimate. Uses `cost_dba_minutes_per_request`, `cost_dba_hourly_usd`, `cost_avoided_replicas`, `cost_per_replica_monthly_usd`, `cost_other_monthly_usd`. |
| `p_metrics_volume_daily` / `_weekly` / `_monthly` | Request counts bucketed by `created_at`, broken down by terminal status. |
| `p_metrics_usage_daily` | Single daily-usage feed for dashboards: submitted + per-status counts (incl. `awaiting_dba_manual` + scheduled), active_users, distinct targets touched, total rows returned, mean/p95 execution latency, mean approval latency, and any `metric_annotations` from that day. |
| `p_metrics_team_usage` | Per-team request volume + tier mix. |
| `p_metrics_top_users` | Top requesters by volume (last 90 days). |
| `p_metrics_scheduled_usage` | How often the scheduling feature is actually used (scheduled vs immediate, cancellation rate). |
| `p_metrics_tier_distribution` | RO vs RW vs DDL submission mix. |
| `p_metrics_failure_breakdown` | Failed requests grouped by error class. |
| `p_metrics_admin_workload` | Decisions per admin, median time-to-decision. |
| `p_metrics_target_heatmap` | (target, day) request volume — surfaces hot targets. |
| `p_metrics_peak_hours` | Hour-of-day distribution in `report_timezone`. |
| `p_metrics_business_vs_offhours` | Share of requests in / out of business hours. |
| `p_metrics_approval_sla` | Time-to-approval percentiles. |
| `p_metrics_rating_weekly` | Weekly rating rollup: n, avg, low (≤2), high (≥4), with_feedback. |
| `p_metrics_rating_response_rate` | Of all terminal-state requests, what fraction got a rating. |
| `p_metrics_rating_low_with_feedback` | Drill-down on 1-2 ratings with the original query preview. |
| `p_metrics_who_can_what` | One row per active user with `is_admin` (+ `admin_max_tier` / `admin_scope_*`), `is_bypass`, `teams[]`, `user_grants[]`. Powers the `/sql roles` slash sub-command. |

---

## Enum types

### `request_status`

Lifecycle of a `requests` row:

```
pending → approved   → executing → completed | failed
        → scheduled  → executing → completed | failed
        → scheduled  → cancelled                       (via [Cancel] button)
        → rejected
        → changes_requested

  executing (DDL) → awaiting_dba_manual → completed | failed
                                          (via admin [Mark completed] / [Mark failed])
```

- `approved` (immediate): admin clicks Approve and `scheduled_for` is NULL or in the past → handler dispatches to executor right away.
- `scheduled`: admin clicks Approve and `scheduled_for` is in the future. The bot's scheduler thread polls every 60s, picks up rows whose time is due, flips them to `executing`, and dispatches.
- `cancelled`: requester or admin clicks [Cancel] on the scheduled DM before it runs. The scheduler will skip rows in this state (it only matches `status='scheduled'`).
- `rejected` / `changes_requested`: admin chose those buttons.
- `awaiting_dba_manual`: executor hit Postgres `InsufficientPrivilege` (SQLSTATE 42501) on a DDL statement — typically because the bot's DDL role doesn't own the object. The request parks here; a DBA runs the change out-of-band and closes the request from Slack using [Mark completed] or [Mark failed].

Auto-approved requests skip the `pending` step entirely — they
INSERT with `status='approved'` (or `'scheduled'`) directly,
`decided_by_slack_id='AUTO'`, and `audit_log` carries both a
`submitted` row and an `auto_approved` row.

### `bundle_status`

Rollup of the per-item statuses in a `/sql batch` submission.
Maintained automatically by the AFTER UPDATE trigger on
`requests.status` (`trg_recompute_bundle_status`):

```
pending   → any item still pending / approved / scheduled /
            executing / awaiting_dba_manual / changes_requested
partial   → every item terminal, but mix of completed + at least
            one rejected / failed / cancelled
decided   → every item terminal AND no negative outcomes
            (all completed, all rejected, all failed)
cancelled → every item cancelled
```

The trigger uses `pg_advisory_xact_lock(bundle_id)` to serialise
concurrent recomputes so two items finishing at the same moment
can't both see "sibling still executing" and leave the bundle
stuck at `pending`.

---

## Indexes worth knowing about

- `idx_requests_pending` — partial on `status IN (pending, approved, executing)`. Speeds up the "what's outstanding" admin views.
- `idx_requests_pending_cleanup` — partial on `slack_file_id IS NOT NULL`. Speeds up the cleanup script's "find expired uploads" query.
- `uq_access_requests_pending` — unique partial on `(requester, target, md5(query)) WHERE status = 'pending'`. Enforces the "one pending per (user, target, query)" rule without blocking re-requests after a decision.
- `idx_team_target_grants_target` — looks up "which teams reach this target".
- `idx_requesters_enabled` — fast allowlist check on every `/sql`.

---

## How the tables fit together

```
                ┌──────────┐
                │ admins   │   bypass requesters + team checks
                └──────────┘

  /sql ⮕  ┌──────────────┐       ┌──────────────────────┐
          │ requesters   │  ←──  │ access_requests      │
          │ (allowlist)  │       │ (request access UX)  │
          └──────────────┘       └──────────────────────┘
                  ↓
          ┌───────┴────────┐
          │ team_members   │
          └───────┬────────┘
                  │
          ┌───────┴────────────┐       ┌──────────────────┐
          │ team_target_grants │  →    │ target_servers   │
          └────────────────────┘       └──────────────────┘
                                              │
              ┌───────────────────────────────┘
              ↓
       ┌──────────────┐       ┌────────────────────────┐
       │ requests     │  ←──  │ request_notifications  │
       │ (audit)      │       │ (admin DM tracking)    │
       └──────┬───────┘       └────────────────────────┘
              │
              ↓
       ┌──────────────┐
       │ audit_log    │
       └──────────────┘
```
