-- Seed the bot_config keys the code reads but no migration ever created.
--
-- 29 of them. The effect of a missing row is not a broken feature — every read
-- passes a default, so behaviour is fine — it is that the key is INVISIBLE:
-- GET /admin/config builds its groups from the rows in this table, so an
-- operator could not see or change any of these from the admin UI, including
-- the target-TLS and trusted-proxy hardening switches. The only way to set one
-- was an INSERT by hand, which nothing documented.
--
-- Values below are the code's own defaults, extracted from the get_setting /
-- get_int / get_bool call sites, so applying this changes NO behaviour: it makes
-- the current behaviour visible and editable. ON CONFLICT DO NOTHING, so an
-- install that already set one of these by hand keeps its value.
--
-- If you add a new bot_config read, seed it here (or in a later migration).
-- tests/test_config_keys_seeded.py fails the build when a key is read but not
-- seeded, so this cannot quietly drift again.

INSERT INTO bot_config (key, value, description) VALUES

-- ---------- SQL safety / execution ----------
('allow_explain_analyze', 'off',
 'Allow EXPLAIN ANALYZE at submit time. ANALYZE actually RUNS the statement, so this is off by default: an EXPLAIN ANALYZE of a DELETE deletes.'),
('explain_inline_plan', 'on',
 'Show the query plan inline on the approval card instead of as an attachment.'),
('explain_max_chars', '11000',
 'Truncate an inline plan longer than this (Slack block limit is 3000 per section; this budgets across sections).'),
('csv_size_mb_ceiling', '100',
 'Hard ceiling for a result file in MB. The per-user row limit scales up to this; a query that would exceed it fails and DMs the requester plus the admins.'),

-- ---------- PII ----------
('pii_region', 'generic',
 'Which content-detector pack to run: generic (IBAN/card/email/E.164) or a region pack that adds national identifiers. Comma-separated to combine.'),

-- ---------- notifications ----------
('auto_approve_feed_channel', '',
 'Channel id that receives a feed of auto-approved queries. Empty = no feed.'),
('service_restart_dm', 'off',
 'DM admins "back online" when the bot process starts. Off by default: a restart during a deploy would otherwise DM everyone.'),
('auth_event_dm_enabled', 'on',
 'DM users when their own authorization changes (grant/revoke), including changes made directly in SQL by an operator.'),
('auth_event_poll_seconds', '20',
 'How often the authorization-event poller drains auth_event_outbox.'),

-- ---------- access requests ----------
('max_open_access_requests_per_user', '5',
 'Cap on simultaneously open access requests per user, so one person cannot flood the admin queue.'),

-- ---------- grants ----------
('control_plane_target_ids', '',
 'Comma-separated target ids that reach the bot''s OWN metadata database; granting access to them is refused. Empty = detect automatically from BOT_DB_* (set this explicitly when a pooler, CNAME or proxy hides the match).'),

-- ---------- SQL Server targets ----------
('mssql_odbc_driver', '',
 'ODBC driver name for SQL Server targets, e.g. "ODBC Driver 18 for SQL Server". Empty = auto-detect the newest installed.'),
('mssql_trust_server_cert', 'off',
 'Skip TLS certificate verification for SQL Server connections. Off is correct; turning it on accepts any certificate, including an attacker''s.'),
('mssql_multi_subnet_failover', 'on',
 'Set MultiSubnetFailover=yes so an Availability Group listener fails over to the current primary instead of hanging on a dead replica.'),

-- ---------- AWS Secrets Manager provider ----------
('awssm_cache_ttl_seconds', '60',
 'How long a fetched secret is cached in memory. Longer means fewer API calls but a slower reaction to a rotation.'),

-- ---------- web: auth ----------
('web_auth_slack_enabled', 'on',
 'Offer Slack OIDC on the web sign-in page.'),
('web_allowed_email_domain', '',
 'Restrict web sign-in to these email domains (comma-separated). Empty = no domain restriction beyond the provider''s own.'),
('web_access_token_minutes', '20',
 'Lifetime of the short-lived access JWT. Shorter narrows the window in which a stolen token is usable.'),
('web_refresh_token_hours', '12',
 'Lifetime of the refresh token, i.e. how long a browser stays signed in without re-authenticating.'),
('web_refresh_grace_seconds', '30',
 'Window in which a just-superseded refresh token is treated as a normal two-tab race and rotated again, instead of as token theft. 0 = strict single-use.'),
('web_local_login_max_failures', '5',
 'Failed local-login attempts before the account is throttled.'),
('web_local_login_window_minutes', '15',
 'Window over which failed local logins are counted, and how long the throttle lasts.'),

-- ---------- web: deployment ----------
('web_base_url', 'http://localhost:8080',
 'Externally visible origin of the web app. Used for the OAuth redirect and for links in notifications; must match the redirect URL registered with the identity provider.'),
('web_cookie_secure', 'off',
 'Set the Secure flag on session cookies. Turn ON for any deployment served over HTTPS; off exists only for local http development.'),
('web_trusted_proxy', 'off',
 'Trust X-Forwarded-For for the client IP. Leave off unless the app is behind a proxy you control — otherwise a client can forge its own address in the audit log.'),
('web_org_label', 'QueryHub',
 'Organisation name shown on the sign-in page ("Restricted to <org>").'),
('web_display_timezone', 'UTC',
 'Timezone for timestamps in the web UI. Storage is always UTC; this is display only.'),

-- ---------- web: results ----------
('web_result_max_rows', '1000',
 'Rows the web UI loads into the result grid per page. Larger pages are slower to render; the full result is still downloadable.'),
('web_result_to_slack', 'off',
 'Also deliver the result file to Slack for queries submitted from the web. Off keeps a web submission''s output in the web app.')

ON CONFLICT (key) DO NOTHING;
