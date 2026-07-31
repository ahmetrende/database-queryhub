-- Operational metrics endpoint: GET /metrics in Prometheus text format.
--
-- Two keys, both defaulting to the closed position.
--
-- `web_metrics_enabled` is 'off' because a self-hosted tool must not begin
-- exposing its queue depth, fleet size and user counts because somebody
-- upgraded. Turning it on is a deployment decision. While off the route
-- answers 404 rather than 403 — a 403 confirms the endpoint is there.
--
-- `web_metrics_token` is empty, and empty means "require an admin session".
-- That is deliberate: enabling the endpoint without also setting a token must
-- not publish it. Set the token when a scraper needs it — Prometheus can send
-- `Authorization: Bearer <token>` but cannot hold a session cookie. Compared
-- with hmac.compare_digest, so it is a real credential; generate it the way you
-- would any other:
--
--   python -c 'import secrets; print(secrets.token_urlsafe(32))'
--
-- The metrics themselves are computed from SQL at scrape time rather than from
-- in-process counters, so they survive restarts and are identical whichever
-- process is scraped. Nothing to seed for that.

INSERT INTO bot_config (key, value, description) VALUES
('web_metrics_enabled', 'off',
 'Serve GET /metrics in Prometheus text format. ''off'' (default) makes the '
 'route answer 404. Metrics cover queue depth and age, request totals by '
 'status/origin/tier, approval and execution time, fleet and grant counts, the '
 'kill switch, and the auth-event outbox backlog.'),
('web_metrics_token', '',
 'Bearer token for GET /metrics, for scrapers that cannot hold a session '
 'cookie. Empty (default) means an admin session is required instead — so '
 'enabling the endpoint without setting a token does not publish it. Generate '
 'with: python -c ''import secrets; print(secrets.token_urlsafe(32))''')
ON CONFLICT (key) DO NOTHING;
