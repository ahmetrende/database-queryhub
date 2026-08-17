# Configuration reference

Almost all of QueryHub's behavior is controlled by rows in the `bot_config`
table, not by code or env vars. Each row is a `key` / `value` string pair;
the app reads them with typed accessors that supply the default shown below
when the row is absent.

- **Runtime-effective:** every key here is read on the relevant request or
  loop tick, so changing a value takes effect without a restart. The one
  exception is `log_level` (read once at process start). A few keys that
  configure a background thread's cadence (e.g. `auth_event_poll_seconds`)
  are read when that thread starts.
- **Booleans** accept `on`/`off` (also `1`/`true`/`yes`). Integers are
  parsed as-is.
- Set a value with an upsert:

  ```sql
  INSERT INTO bot_config (key, value) VALUES ('max_rows', '5000')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
  ```

Keys marked **(Slack)** only matter when the Slack surface is installed;
they are inert in the vanilla (web-only) profile.

## Web UI & authentication

| Key | Default | What it does |
|---|---|---|
| `web_auth_slack_enabled` | `on` | Offer Slack SSO on the sign-in screen. **(Slack)** |
| `web_auth_local_enabled` | `off` | Offer built-in username/password login (local accounts). Turn on for the vanilla profile. |
| `web_auth_<id>_enabled` | `on` | Offer the external OIDC provider `<id>` (one row per provider you configured via `OIDC_<ID>_*` in the environment — see AUTH.md §1.1). Defaults to on because setting the secrets is the deliberate act; this switch turns a working provider off. |
| `web_auth_<id>_label` | `""` | Button text for that provider. Falls back to `OIDC_<ID>_LABEL`, then `Sign in with SSO`. |
| `web_local_login_max_failures` | `5` | Failed local-login attempts (per username and per IP) tolerated inside the window before a 429 lockout. |
| `web_local_login_window_minutes` | `15` | Sliding window for the failure counter; the lock lifts as failures age out. |
| `web_allowed_email_domain` | `""` | If set, restrict Slack SSO and external-OIDC logins to this email domain (e.g. `example.com`). Empty = no domain gate. |
| `auth_session_retention_days` | `7` | Expired/revoked login sessions (`web_sessions`) are deleted after this many days. Nothing removed them before, so the table grew for every sign-in. |
| `auth_outbox_retention_days` | `14` | Processed authorization-change outbox rows are deleted after this many days. |
| `web_refresh_grace_seconds` | `30` | How long a just-rotated refresh token still works, so two tabs refreshing at once are not treated as token theft. 0 = strict single-use. |
| `control_plane_target_ids` | *(auto)* | Comma-separated target ids that reach the bot's own metadata DB and can never be granted. Empty = detect from the configured BOT_DB_* connection. |
| `web_base_url` | `http://localhost:8080` | External origin used for OAuth redirects and links. Override per-process with the `WEB_BASE_URL` env var. |
| `web_cookie_secure` | `auto` | Set the `Secure` flag on session cookies. `auto` derives it from `web_base_url` — `https` means Secure. `on`/`off` override in either direction; use `off` only when TLS terminates at a proxy and the app itself is reached over plain HTTP. |
| `web_trusted_proxy` | `off` | Trust `X-Forwarded-For` for the client IP. Leave **off** unless the app sits behind a proxy you control — otherwise a client can forge the address that lands in the audit log. |
| `web_trusted_proxy_hops` | `1` | How many proxies sit in front, when `web_trusted_proxy` is on. The client address is read that many entries from the **right** of `X-Forwarded-For`, because each proxy appends and only the rightmost entries were written by infrastructure you control. `1` = a single proxy; `2` = proxy behind proxy. |
| `web_access_token_minutes` | `20` | Lifetime of the short access JWT. |
| `web_refresh_token_hours` | `12` | Lifetime of the refresh token (the session's outer bound). |
| `web_display_timezone` | `UTC` | Zone the UI formats every timestamp in (the DB always stores UTC). |
| `awssm_cache_ttl_seconds` | `60` | How long a secret fetched from AWS Secrets Manager is held in memory. Longer means fewer API calls; shorter reacts faster to a rotation. Only used when a target's secrets provider is `awssm`. |
| `web_metrics_enabled` | `off` | Serve `GET /metrics` in Prometheus text format (queue depth and age, request totals, approval/execution time, fleet and grant counts, kill switch, auth-event outbox backlog). While `off` the route answers **404**, not 403 — a 403 confirms the endpoint exists. |
| `web_metrics_token` | `""` | Bearer token for `GET /metrics`, for scrapers that cannot hold a session cookie. Empty means an **admin session** is required instead, so enabling the endpoint without setting a token does not publish it. Compared in constant time. |
| `web_org_label` | `QueryHub` | Org name shown in the nav (cosmetic). |

### Environment variables (not `bot_config`)

A few settings cannot live in `bot_config`, because reading `bot_config`
requires the database connection they configure:

| Env var | Default | Meaning |
| --- | --- | --- |
| `QH_DB_POOL_MIN` | `1` | Minimum metadata connections held open. |
| `QH_DB_POOL_MAX` | `10` | Maximum metadata connections. Raise if web latency climbs while the target databases are idle — that is request threads queueing for a metadata connection. Every web route is synchronous, so uvicorn's threadpool (40 by default) is the upstream ceiling. |
| `QH_WEB_STATIC_DIR` | *(unset)* | Serve the frontend from this directory instead of `QueryHubWeb/app/dist`. |
| `WEB_BASE_URL` | *(unset)* | Per-process override for `web_base_url`, for a second instance on the same database. |
| `LOG_LEVEL` | `INFO` | Root log level. Read once at process start — a change needs a restart. |
| `LOG_FORMAT` | `text` | `json` emits one JSON object per line (`timestamp`/`level`/`logger`/`message`, plus any `extra=` fields and a single-line `exception`) for a log pipeline. `text` stays human-readable for `journalctl`. Also read once at start. |

Invalid or out-of-range pool values log a warning and fall back to the
default rather than stopping the process.
| `web_result_max_rows` | `1000` | Max rows the web result grid pages through. |
| `web_result_to_slack` | `false` | Also deliver a web-submitted query's result to Slack. **(Slack)** |
| `web_repo_slug` | `""` | `owner/repo` to turn changelog commit SHAs into GitHub links. Empty = plain SHAs. |
| `web_changelog_path` | `""` | Path to an external changelog JSON feeding the in-app What's-new page. |


> **Adding a key.** Seed it in a migration with its code default and a
> description — `GET /admin/config` builds the admin UI from the rows in
> `bot_config`, so a key that is only read in code exists but cannot be seen or
> changed by an operator. `tests/test_config_keys_seeded.py` fails the build if a
> key is read without being seeded; migration 079 is the pattern to copy.

## Query execution, safety & limits

| Key | Default | What it does |
|---|---|---|
| `kill_switch` | `off` | Master stop: reject all new submissions. |
| `kill_switch_message` | _(notice)_ | Message shown while the kill switch is on. |
| `query_timeout_sec` | `300` | Statement timeout for an executing query. |
| `execution_lease_sec` | `900` | How long a claimed execution lease is held before it is considered stale. |
| `max_rows` | `1000` | Row cap on a delivered result set (Slack path). |
| `super_admin_max_rows` | `0` | Row-cap **floor** for super-admins, applied as `max(derived, this)`. `0` = inert. It can never lower anyone's cap. |
| `super_admin_max_mb` | `0` | Size-cap **floor** in MB for super-admins, applied after `csv_size_mb_ceiling`, which it outranks. `0` = inert. Needed because the size cap is otherwise *derived* from the row cap (`csv_size_mb × rows / max_rows`, trimmed by the ceiling), so bytes were not expressible on their own — and a ceiling only ever trims. |
| `max_open_requests_per_user` | `5` | Max simultaneously pending requests one user may have. |
| `min_query_length` | `6` | Reject trivially short queries. |
| `ast_safety_enabled` | `on` | Run the sqlglot AST safety second pass (on top of the leading-keyword allow-list). |
| `set_allowed_params` | `""` | Comma list of `SET LOCAL` parameters a query may set (validated by type/range). Empty = none. |
| `query_plan_logging` | `off` | Log EXPLAIN plans of executed queries. |
| `risk_high_cost` | `50000` | EXPLAIN total-cost above which a submit is flagged high-risk. |
| `risk_seq_scan_rows` | `100000` | Estimated seq-scan rows above which a submit is flagged. |

## Pre-flight & EXPLAIN

| Key | Default | What it does |
|---|---|---|
| `pre_flight_explain` | `on` | Run EXPLAIN at submit time for a cost/risk hint. |
| `explain_inline_plan` | `on` | Show the plan inline in the submit UX. |
| `explain_max_chars` | `11000` | Truncate very large plans to this many characters. |
| `allow_explain_analyze` | `off` | Permit `EXPLAIN (ANALYZE)` (actually executes the query). |

## PII masking

| Key | Default | What it does |
|---|---|---|
| `pii_masking_enabled` | `on` | Mask columns flagged as PII in delivered results. Leave on. |
| `pii_region` | `generic` | Content-detector pack. `generic` is country-neutral: email, Luhn-checked card, IBAN for every ISO 13616 country, E.164 phone. `tr` adds the Turkish national id and tax number — each matches **any** 11/10-digit run and is separated from ordinary numbers only by a national checksum, so roughly a tenth of arbitrary 10-digit values would be mangled where it doesn't apply. Set it to the region you actually operate in. |

## Approvals, auto-approve & RO burst

| Key | Default | What it does |
|---|---|---|
| `fingerprint_cache_enabled` | `on` | Auto-approve a re-submission identical to a previously approved query. |
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
| `max_schedule_days` | `7` | Furthest out a query may be scheduled. |
| `csv_import_enabled` | `off` | Allow CSV → table imports. |
| `csv_size_mb` | `10` | Default result CSV size cap. |
| `csv_size_mb_ceiling` | `100` | Hard ceiling the size cap can scale up to for large results. |
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
| `auth_event_dm_enabled` | `on` | DM users on any grant/revoke change affecting them. |
| `auth_event_poll_seconds` | `20` | Poll cadence for the auth-event outbox (read when the poller thread starts). |

## SQL Server (MSSQL) targets

| Key | Default | What it does |
|---|---|---|
| `mssql_odbc_driver` | `""` | ODBC driver name (e.g. `ODBC Driver 18 for SQL Server`). Required for MSSQL targets. |
| `mssql_multi_subnet_failover` | `true` | Set `MultiSubnetFailover=yes` on the connection. |
| `mssql_trust_server_cert` | `false` | Trust a self-signed server certificate. |

---

Grants, admins, teams and targets are **not** in `bot_config` — they live in
their own tables (`team_target_grants`, `user_target_grants`, `admins`,
`teams`, `target_servers`, …). See [OPERATIONS.md](OPERATIONS.md).
