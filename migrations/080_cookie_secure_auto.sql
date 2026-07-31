-- Session cookies: derive `Secure` from the deployment instead of failing open.
--
-- The documented install produced an HTTPS deployment with non-Secure session
-- cookies. scripts/install.sh generates a certificate, sets WEB_SSL_CERTFILE,
-- and prints "open https://localhost:8080" — and never touches
-- web_cookie_secure, whose default was 'off'. docs/CONFIGURATION.md said to
-- turn it on in any real deployment; nothing enforced it and nothing warned,
-- and an operator installing a tool does not read every config row first.
--
-- The code now derives the flag when the key is not an explicit on/off:
-- `https` in web_base_url means Secure. Migration 079 seeded the key with the
-- literal 'off', though, so on a fresh install the derivation would never be
-- reached — the seeded value would answer first. Hence this migration, and
-- hence a third value.
--
-- 'auto' means "decide from web_base_url". An explicit 'on' or 'off' still wins
-- in both directions: an operator terminating TLS at a proxy and deliberately
-- serving the app over plain HTTP internally can still say 'off'.
--
-- Why it is safe to rewrite an existing 'off':
--   * http deployment  -> 'auto' derives false -> unchanged behaviour.
--   * https deployment -> 'auto' derives true  -> the cookie gains Secure,
--     which is the entire point; a client reaching an https deployment cannot
--     be harmed by it.
-- An operator who has already chosen 'on' is untouched by the WHERE clause.
-- Migrations are append-only here, so 079 stays as it was written.

UPDATE bot_config
   SET value = 'auto',
       description =
         'Set the Secure flag on session cookies. ''auto'' (default) derives it '
         'from web_base_url: https means Secure. ''on''/''off'' override in '
         'either direction — use ''off'' only when TLS terminates elsewhere and '
         'the app itself is reached over plain HTTP.',
       updated_at = NOW()
 WHERE key = 'web_cookie_secure'
   AND value = 'off';

-- Present the key on installs that somehow never got 079.
INSERT INTO bot_config (key, value, description) VALUES
('web_cookie_secure', 'auto',
 'Set the Secure flag on session cookies. ''auto'' (default) derives it from '
 'web_base_url: https means Secure. ''on''/''off'' override in either direction.')
ON CONFLICT (key) DO NOTHING;

-- And the hop count for X-Forwarded-For, which had no key at all.
--
-- The leftmost entry was being read as the client address. nginx's standard
-- `proxy_add_x_forwarded_for` APPENDS the real peer to whatever the client
-- sent, so `X-Forwarded-For: 9.9.9.9` arrives as `9.9.9.9, <real-ip>` and the
-- leftmost value is the attacker's own string — which then keys the per-IP
-- login throttle and lands in audit_log. The right-hand end is the trustworthy
-- one, and how far in to step depends on how many proxies are in front.
INSERT INTO bot_config (key, value, description) VALUES
('web_trusted_proxy_hops', '1',
 'How many reverse proxies sit in front, when web_trusted_proxy is on. The '
 'client address is read that many entries from the RIGHT of X-Forwarded-For, '
 'because each proxy appends and only the rightmost entries were written by '
 'infrastructure you control. 1 = a single proxy (default); 2 = proxy behind '
 'proxy. Reading from the left would take the client-supplied value.')
ON CONFLICT (key) DO NOTHING;
