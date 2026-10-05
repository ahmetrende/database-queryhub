# Configuration reference

Rows in the `bot_config` table control almost all of QueryHub's behavior,
not code or env vars. Each row is a `key` / `value` string pair. The app
reads the rows with typed accessors. When a row is absent, the accessor
supplies the default shown below.

- **Runtime-effective:** the app reads every key here on the relevant
  request or loop tick. A changed value takes effect without a restart.
  The one exception is `log_level` (read once at process start). The app
  reads a few keys that set a background thread's cadence (e.g.
  `auth_event_poll_seconds`) when that thread starts.
- **Booleans** accept `on`/`off` (also `1`/`true`/`yes`). The app parses
  integers as-is.
- Set a value with an upsert:

  ```sql
  INSERT INTO bot_config (key, value) VALUES ('max_rows', '5000')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
  ```

Keys marked **(Slack)** only matter when the Slack surface is installed.
They are inert in the vanilla (web-only) profile.

## Web UI & authentication

| Key | Default | What it does |
|---|---|---|
| `web_auth_slack_enabled` | `on` | Offer Slack SSO on the sign-in screen. **(Slack)** |
| `web_auth_local_enabled` | `off` | Offer built-in username/password login (local accounts). Enable it for the vanilla profile. |
| `web_auth_<id>_enabled` | `on` | Offer the external OIDC provider `<id>`. There is one row per provider that you configured through `OIDC_<ID>_*` in the environment (see AUTH.md §1.1). The default is on, because the deliberate act is to set the secrets. This switch disables a working provider. |
| `web_auth_<id>_label` | `""` | Button text for that provider. If it is empty, the button uses `OIDC_<ID>_LABEL`, then `Sign in with SSO`. |
| `web_local_login_max_failures` | `5` | Failed local-login attempts (per username and per IP) that the app tolerates inside the window, before a 429 lockout. |
| `web_local_login_window_minutes` | `15` | Sliding window for the failure counter. The lock ends as the failures leave the window. |
| `web_slack_team_id` | `""` | The Slack workspace (team id, `T…`) that a Slack sign-in must come from. Empty = the bot's own workspace, found with `auth.test`. If the app can establish neither, it refuses the sign-in. So an install that runs Slack sign-in without the bot must set this key. **(Slack)** |
| `web_allowed_email_domain` | `""` | If set, restrict Slack SSO and external-OIDC logins to this email domain (e.g. `example.com`). Empty = no domain gate. |
| `auth_session_retention_days` | `7` | The app deletes expired/revoked login sessions (`web_sessions`) after this many days. Nothing deleted them before, so the table grew with every sign-in. |
| `auth_outbox_retention_days` | `14` | The app deletes processed authorization-change outbox rows after this many days. |
| `idp_assertion_enabled` | `off` | Accept identity assertions from a trusted portal (AUTH.md §1.2). Every `idp_*` setting below is inert while this is off. |
| `idp_issuer` / `idp_audience` | `idp` / `queryhub` | The `iss` and `aud` that an assertion must carry. |
| `idp_public_keys` | `{}` | JSON object mapping `kid` to the portal's Ed25519 public key (PEM). Empty = no assertion verifies. |
| `idp_sync_principal` | `""` | The one principal allowed to call the reconcile and the notification outbox routes, through an assertion. Empty = nobody. |
| `idp_sync_max_disable` | `5` | The most requesters that one portal sync may disable before a super-admin must approve it (AUTH.md §1.2). A run over the limit changes nothing and answers 409. It also sends every super-admin an approve / reject card. `0` = every disabling run needs approval. |
| `idp_clock_skew_seconds` | `10` | Clock disagreement that the app tolerates between the portal and this host, on an assertion's `iat` and `exp` (0-60). |
| `idp_outbox_enabled` | `off` | Write a `notification_outbox` row for the portal on each pending submission. Keep it off until the portal polls the outbox. |
| `idp_outbox_retention_days` | `7` | The daily cleanup deletes `notification_outbox` rows older than this, processed or not. |
| `web_refresh_grace_seconds` | `30` | How long a just-rotated refresh token still works. With it, the app does not treat two tabs that refresh at once as token theft. 0 = strict single-use. |
| `control_plane_target_ids` | *(auto)* | Comma-separated target ids that reach the bot's own metadata DB. Nobody can ever grant them. Empty = detect them from the configured BOT_DB_* connection. |
| `web_base_url` | `http://localhost:8080` | External origin for OAuth redirects and links. Override it per process with the `WEB_BASE_URL` env var. |
| `web_cookie_secure` | `auto` | Set the `Secure` flag on session cookies. `auto` derives it from `web_base_url`: `https` means Secure. `on`/`off` override in either direction. Use `off` only when TLS ends at a proxy and the app itself is reached over plain HTTP. |
| `web_trusted_proxy` | `off` | Trust `X-Forwarded-For` for the client IP. Keep it **off** unless the app sits behind a proxy that you control. Otherwise a client can forge the address that the audit log records. |
| `web_trusted_proxy_hops` | `1` | How many proxies sit in front, when `web_trusted_proxy` is on. The app reads the client address that many entries from the **right** of `X-Forwarded-For`. The reason: each proxy appends an entry, and only the rightmost entries come from infrastructure that you control. `1` = a single proxy. `2` = proxy behind proxy. |
| `web_access_token_minutes` | `20` | Lifetime of the short access JWT. |
| `web_refresh_token_hours` | `12` | Lifetime of the refresh token (the session's outer bound). |
| `web_employment_grace_hours` | `2` | When Slack cannot answer users.info, a sign-in or refresh still passes if Slack called the person active within this many hours. An RW/DDL submit always needs a live answer. `0` = no grace. |
| `web_display_timezone` | `UTC` | Zone the UI formats every timestamp in (the DB always stores UTC). |
| `awssm_cache_ttl_seconds` | `60` | How long the app holds a secret fetched from AWS Secrets Manager in memory. Longer means fewer API calls. Shorter reacts faster to a rotation. The app uses it only when a target's secrets provider is `awssm`. |
| `web_metrics_enabled` | `off` | Serve `GET /metrics` in Prometheus text format: queue depth and age, request totals, approval/execution time, fleet and grant counts, kill switch, auth-event outbox backlog. While it is `off`, the route answers **404**, not 403. A 403 tells a caller that the endpoint exists. |
| `web_metrics_token` | `""` | Bearer token for `GET /metrics`, for scrapers that cannot hold a session cookie. Empty means that the route requires an **admin session** instead. So if you enable the endpoint and set no token, the endpoint stays unpublished. The app compares the token in constant time. |
| `web_org_label` | `QueryHub` | Org name shown in the nav (cosmetic). |

### Environment variables (not `bot_config`)

A few settings cannot live in `bot_config`. To read `bot_config`, the app
needs the database connection that they configure:

| Env var | Default | Meaning |
| --- | --- | --- |
| `QH_DB_POOL_MIN` | `1` | Minimum metadata connections held open. |
| `QH_DB_POOL_MAX` | `10` | Maximum metadata connections. Raise it if web latency climbs while the target databases are idle. That pattern means that request threads queue for a metadata connection. Every web route is synchronous, so uvicorn's threadpool (40 by default) is the upstream ceiling. |
| `BOT_DB_SSLMODE` | *(unset)* | libpq `sslmode` for the metadata DB and the inventory DB on the same server. Unset keeps libpq's default (`prefer`). `verify-full` needs `BOT_DB_SSLROOTCERT`. |
| `BOT_DB_SSLROOTCERT` | *(unset)* | CA file for `BOT_DB_SSLMODE`. Do not use libpq's `PGSSLROOTCERT` instead. libpq applies it to target connections too, and a root file turns their `require` into `verify-ca` against the wrong CA. |
| `QH_WEB_STATIC_DIR` | *(unset)* | Serve the frontend from this directory instead of `QueryHubWeb/app/dist`. |
| `BOT_DB_MIGRATOR_USER`, `BOT_DB_MIGRATOR_PASSWORD`, `BOT_DB_OWNER_ROLE` | *(unset)* | For `scripts/apply_migrations.py`, after the metadata roles are split (OPERATIONS.md §28). The script connects as the migrator and runs `SET ROLE` to the owner. Do not put these variables in the services' environment. |
| `QH_BUILD_SHA`, `QH_BUILD_VERSION` | *(unset)* | Build identity for the build stamp when there is no `.git` to ask, as in the container image. The release workflow sets both. |
| `QH_IMAGE_DIGEST` | *(unset)* | Shown in the build stamp and the startup log line. Set it where the image runs, from `docker inspect`: the image cannot know its own digest. |
| `WEB_SESSION_SECRET` | *(unset)* | Session signing key. If it is unset, the app derives one from the master key, which is the right default. An override shorter than 32 bytes stops the web process at startup. |
| `QH_SECRETS_PLAINTEXT_FALLBACK` | *(unset)* | `1` lets a process start from the plaintext environment when `secrets.enc` exists but is unreadable. Without it, that case is fatal. Use it only for the move to the encrypted file. |
| `WEB_BASE_URL` | *(unset)* | Per-process override for `web_base_url`, for a second instance on the same database. |
| `LOG_LEVEL` | `INFO` | Root log level. The app reads it once at process start, so a change needs a restart. |
| `LOG_FORMAT` | `text` | `json` emits one JSON object per line for a log pipeline: `timestamp`/`level`/`logger`/`message`, plus any `extra=` fields and a single-line `exception`. `text` stays human-readable for `journalctl`. The app also reads it once at start. |

An invalid or out-of-range pool value logs a warning, and the app uses the
default instead of stopping the process.
| `web_result_max_rows` | `1000` | Max rows the web result grid pages through. |
| `web_result_to_slack` | `false` | Also deliver a web-submitted query's result to Slack. **(Slack)** |
| `web_repo_slug` | `""` | `owner/repo` to turn changelog commit SHAs into GitHub links. Empty = plain SHAs. |
| `web_changelog_path` | `""` | Path to an external changelog JSON that feeds the in-app What's-new page. |


> **Adding a key.** Seed it in a migration with its code default and a
> description. `GET /admin/config` builds the admin UI from the rows in
> `bot_config`. So an operator cannot see or change a key that only the code
> reads, although the key exists. `tests/test_config_keys_seeded.py` fails
> the build if the code reads a key that no migration seeds. Migration 079 is
> the pattern to copy.

## Query execution, safety & limits

| Key | Default | What it does |
|---|---|---|
| `kill_switch` | `off` | Master stop: reject all new submissions. |
| `kill_switch_message` | _(notice)_ | Message shown while the kill switch is on. |
| `query_timeout_sec` | `300` | Statement timeout for an executing query. |
| `execution_lease_sec` | `900` | How long a claimed execution lease lasts before the app considers it stale. |
| `max_rows` | `1000` | Row cap on a delivered result set (Slack path). |
| `super_admin_max_rows` | `0` | Row-cap **floor** for super-admins, applied as `max(derived, this)`. `0` = inert. It can never lower anyone's cap. |
| `super_admin_max_mb` | `0` | Size-cap **floor** in MB for super-admins. The app applies it after `csv_size_mb_ceiling`, which it outranks. `0` = inert. The key exists because the size cap is otherwise *derived* from the row cap (`csv_size_mb × rows / max_rows`, trimmed by the ceiling). So bytes were not expressible on their own, and a ceiling only ever trims. |
| `max_open_requests_per_user` | `5` | Max simultaneously pending requests one user may have. |
| `min_query_length` | `6` | Reject trivially short queries. |
| `ast_safety_enabled` | `on` | Run the sqlglot AST safety second pass (in addition to the leading-keyword allow-list). |
| `set_allowed_params` | `""` | Comma list of `SET LOCAL` parameters that a query may set. The app verifies the type/range of each value. Empty = none. |
| `query_plan_logging` | `off` | Log EXPLAIN plans of executed queries. |
| `risk_high_cost` | `50000` | EXPLAIN total cost above which the app flags a submit as high-risk. |
| `risk_seq_scan_rows` | `100000` | Estimated seq-scan rows above which the app flags a submit. |
| `<engine>_blocked_functions` | `""` | Comma-separated function names that a read-only engine refuses, e.g. `clickhouse_blocked_functions`. They add to the functions that the code already refuses, and they cannot re-allow any of them. |

## Pre-flight & EXPLAIN

| Key | Default | What it does |
|---|---|---|
| `pre_flight_explain` | `on` | Run EXPLAIN at submit time for a cost/risk hint. |
| `explain_inline_plan` | `on` | Deliver a lone `EXPLAIN` as an inline code block. Either way, the app also stores the plan as a one-column result file, so a non-Slack client has something to read. |
| `explain_max_chars` | `11000` | Truncate the inline code block to this many characters (a Slack message limit). The stored file keeps the whole plan. |
| `allow_explain_analyze` | `off` | Permit `EXPLAIN (ANALYZE)`, which actually executes the query. |

## PII masking

| Key | Default | What it does |
|---|---|---|
| `pii_masking_enabled` | `on` | Mask columns flagged as PII in delivered results. Keep it on. |
| `pii_region` | `generic` | Content-detector pack. `generic` is country-neutral: email, card number with a Luhn checksum, IBAN for every ISO 13616 country, E.164 phone. `tr` adds the Turkish national id and tax number. Each one matches **any** 11/10-digit run, and only a national checksum separates it from ordinary numbers. So where the pack does not apply, it would mangle roughly a tenth of arbitrary 10-digit values. Set it to the region that you actually operate in. |

## Approvals, auto-approve & RO burst

| Key | Default | What it does |
|---|---|---|
| `fingerprint_cache_enabled` | `on` | Auto-approve a re-submission that is identical to a previously approved query. This does not apply on an Athena target, unless the target allows auto-approve (see "Amazon Athena targets"). |
| `fingerprint_cache_ttl_days` | `30` | How long a fingerprint stays eligible for auto-approve. |
| `require_justification` | `false` | Require a justification note on every submission. |
| `max_open_access_requests_per_user` | `5` | Cap on pending target-access requests per user. |
| `ro_burst_threshold` | `3` | Read-only submissions within the window that trigger the RO-window nudge. |
| `ro_burst_window_min` | `10` | The RO-burst detection window (minutes). |
| `ro_window_minutes` | `60` | Length of a granted read-only auto-approve window. |
| `auto_approve_feed_channel` | `""` | Slack channel that receives an auto-approve FYI feed. **(Slack)** |

## Batch, scheduling & CSV import

| Key | Default | What it does |
|---|---|---|
| `batch_enabled` | `off` | Allow multi-query batch submissions (one approval round). |
| `batch_max_items` | `5` | Max queries in a batch. |
| `max_schedule_days` | `7` | How far ahead a requester may schedule a query. |
| `csv_import_enabled` | `off` | Allow CSV → table imports. |
| `csv_size_mb` | `10` | Default result CSV size cap. |
| `csv_size_mb_ceiling` | `100` | Hard ceiling that the size cap can scale to for large results. |
| `import_max_mb` | `50` | Max uploaded CSV size. |
| `import_max_rows` | `100000` | Max rows per CSV import. |
| `import_timeout_sec` | `600` | CSV import timeout. |

## Ratings

| Key | Default | What it does |
|---|---|---|
| `rating_enabled` | `on` | Prompt for a 1–5 rating after a request reaches a terminal state. **(Slack)** |

## Slack surface (inert in the vanilla profile)

| Key | Default | What it does |
|---|---|---|
| `bot_display_name` | `""` | Override the bot's Slack display name. Empty = the Slack app default. |
| `bot_display_icon` | `""` | Override the bot's Slack avatar/emoji. |
| `service_restart_dm` | `off` | DM admins "back online" after a service restart. |
| `auth_event_dm_enabled` | `on` | DM users on any grant/revoke change that affects them. |
| `auth_event_poll_seconds` | `20` | Poll cadence for the auth-event outbox (read when the poller thread starts). |
| `grant_expiry_warn_enabled` | `on` | Warn a grant holder before a time-bounded grant lapses. Off leaves expiry silent, which is how it behaved before migration 098. |
| `grant_expiry_warn_hours` | `24,4` | Comma-separated hours before expiry to warn at. Widest first wins, so a grant created with three hours left receives one message, not two. Each warning fires once per grant per deadline. An extension of a grant re-arms them, because the recorded deadline stops matching. Empty means no warnings, while the feature itself stays enabled. |

## Read replicas

A read-only PostgreSQL request runs on a healthy, enabled read replica of its
target (`target_servers.replica_of`). See OPERATIONS.md, "Read replicas".

| Key | Default | What it does |
|---|---|---|
| `replica_routing` | `off` | `on` = read-only requests may run on a read replica. This is the kill switch: `off` sends everything to the primary. |
| `replica_max_lag_seconds` | `10` | The app does not use a replica that is further behind its primary than this. It measures lag against the primary's current WAL position. |
| `replica_health_ttl_seconds` | `15` | How long a process trusts one health probe of a replica. |
| `replica_read_your_writes_minutes` | `5` | After a requester's own RW/DDL request on a target, their reads there stay on the primary this long. `0` = off. |

## Target TLS

These keys decide whether a connection to a target verifies the server's
certificate. The two host lists take globs, comma or space separated. The
app matches them, without regard to case, against the host that the
connection goes to. That is a read replica's own host when a query runs
there. A rollout recipe is in [OPERATIONS.md §27](OPERATIONS.md#27-verifying-target-certificates).

| Key | Default | What it does |
|---|---|---|
| `target_ssl_mode` | `require` | libpq `sslmode` for PostgreSQL targets that no host list names. `require` encrypts but does not verify who answered. `verify-full` verifies the certificate against `target_ssl_rootcert`, host name included. |
| `target_ssl_rootcert` | `""` | CA file on the QueryHub host that verification uses. One file may hold several CAs, one per cloud. The app passes it only with a verifying mode, because libpq would read `require` plus a root file as `verify-ca`. |
| `target_ssl_verify_hosts` | `""` | Hosts whose connections verify the certificate: `verify-full` for PostgreSQL, `TrustServerCertificate=no` for SQL Server. Example: `*.rds.amazonaws.com`. |
| `target_ssl_verify_exempt_hosts` | `""` | Hosts whose connections never verify it, for a server whose certificate cannot be verified. It beats the verify list and a verifying `target_ssl_mode`. |

ClickHouse targets always verify, against the public CA bundle, host name
included. The settings screen refuses two things:

- a verifying setting whose CA file is not readable on the host
- a `target_ssl_mode` that libpq does not know

At startup, each process logs one line that counts the enabled targets that
encrypt but do not verify the certificate.

## SQL Server (MSSQL) targets

| Key | Default | What it does |
|---|---|---|
| `mssql_odbc_driver` | `""` | ODBC driver name (e.g. `ODBC Driver 18 for SQL Server`). Required for MSSQL targets. |
| `mssql_multi_subnet_failover` | `true` | Set `MultiSubnetFailover=yes` on the connection. |
| `mssql_trust_server_cert` | `false` | Trust a self-signed server certificate, for SQL Server targets that neither TLS host list names. |

## Amazon Athena targets

An Athena target has no host to connect to and no stored credential.
`target_servers.engine_config` describes it: one JSON object per target.

| Key | Required | What it is |
|---|---|---|
| `region` | yes | AWS region of the workgroup and the Glue catalog. |
| `workgroup` | yes | Athena workgroup the queries run in. Its scan limit caps every query. |
| `database` | yes | Glue database a query runs in by default. |
| `role_arn` | yes | Read-only role the gateway assumes for every call: Glue, Athena and S3. |
| `catalog` | no | Data catalog. Default `AwsDataCatalog`. |
| `freshness_marker` | no | `s3://bucket/key` of the archive's freshness marker. When set, the approver's hint says how far the archive reaches. |
| `auto_approve` | no | Unset by default: the archive rule below decides, and the fingerprint approval cache never applies. `true` makes the target behave like any other: every waiver and the cache apply, and the role rule does not. `false` lets nothing skip review except a super-admin's own query. |

QueryHub limits auto-approve on Athena, because a query's cost depends on
the partitions that it reads. A fingerprint match ignores the values that
choose those partitions. With the key unset, the archive rule applies.

What can skip review:

- Only a read can skip review. Except for super-admins, nobody holds more
  than RO on the target. No grant row changes. The engine refuses a write
  for everyone. Every screen that names a tier per target reports RO there:
  - the web connection list and editor, and the effective-access views
  - the MCP connection list
  - `/sql whoami`, `/sql roles` and `/sql teams`
- The lead of the team that owns the target auto-approves reads, with or
  without a waiver. The lead is someone with a live `approver` role scoped
  to the target. `scripts/sync_team_approvers.py --source pod-sync` writes
  that role from `target_team`. When an admin changes the owners of one
  connection (Admin → Connections → row menu → Owners), QueryHub writes the
  role for that connection at once.
- So does anyone with an `admin` role (any admin, not only a super-admin).
  So does anyone whose waiver covers the read: a fleet-wide waiver, or one
  that names the target.

What stays under review:

- Everyone else's query goes to an approver: that lead, or an admin.
- The fingerprint approval cache never applies, so an approver reviews a
  member's repeat query again.
- QueryHub refuses a request for an auto-approve window on the target. The
  lead and the admins need none, and a window would exempt a member's reads
  from the lead's review.

QueryHub records a waiver's decision with its `grant_id`, as on any target.
A decision that the role rule made has no waiver to name. So the request's
`decision_reason` reads `auto-approved (archive: owner lead, max_tier=ro)` or
`auto-approved (archive: admin, max_tier=ro)`. The `auto_approved` audit row
carries the same `basis`.

Only a JSON `true` or `false` counts. Any other value means the default. A
super-admin's own query is auto-approved either way. The Slack badge, the
web editor and the connection list say that a query will skip review only
where one of these rules lets it. The key works the same on a PostgreSQL,
SQL Server or ClickHouse target, where the default is `true`.

```sql
UPDATE target_servers
   SET engine_config = COALESCE(engine_config, '{}') || '{"auto_approve": true}'
 WHERE alias = 'example-archive';
```

The freshness marker is one JSON object that the archive writes with a single
PutObject:

- `covered_through`: every row up to this moment is in the archive. ISO-8601
  with a time zone (`Z` or an offset).
- `computed_at`: when the archive wrote the marker, in the same format.
- `known_gaps` (optional): `[{"from": ..., "to": ...}]`, holes with known bounds.

QueryHub ignores other fields. The hourly catalog refresh reads the marker
for every Athena target that names one, enabled or not. It stores the
verdict in `target_servers.archive_freshness` (migration 140). The hint then
says "Archive complete up to 25 Aug 2026 12:00 UTC." and lists any known
gaps.

The hint says "Archive coverage unknown." in three cases:

- There is no marker.
- S3 refuses the read.
- The app cannot read `covered_through` or `known_gaps` in full.

The assumed role needs `s3:GetObject` on the marker, and `s3:ListBucket` on
its bucket. Without `s3:ListBucket`, S3 answers a missing marker with a
refusal. That refusal reads as unreadable, not absent.

```sql
UPDATE target_servers
   SET engine_config = engine_config
       || '{"freshness_marker": "s3://example-archive/balance/_meta/ledgers-watermark.json"}'
 WHERE alias = 'example-archive';
```

| Key | Default | What it does |
|---|---|---|
| `athena_freshness_stale_hours` | `36` | Hours after the marker's `computed_at` before the hint adds "The freshness marker has not been updated for N hours" (in days from 48 hours). The app computes it when it builds the hint, so a change applies from the next submission. |

---

Grants, admins, teams and targets are **not** in `bot_config`. They live in
their own tables (`team_target_grants`, `user_target_grants`, `admins`,
`teams`, `target_servers`, …). See [OPERATIONS.md](OPERATIONS.md).
