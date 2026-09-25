-- 133: verify target certificates one host, or one cloud, at a time.
--
-- `target_ssl_mode` was the only switch: one mode for every PostgreSQL target.
-- A fleet that spans two clouds has two certificate authorities, so
-- verify-full could only be turned on for all of it at once, with one CA
-- file, and every server signed by the other CA would stop connecting. Two
-- host-glob lists now set the mode per server (config.target_ssl_kwargs), and
-- the same lists decide SQL Server's certificate check.
--
-- Seeded so the settings screen shows them. Every value keeps today's
-- behavior: require everywhere, no CA file, no host named.
INSERT INTO bot_config (key, value, description) VALUES
  ('target_ssl_mode', 'require',
   'libpq sslmode for PostgreSQL targets no TLS host list names. require = '
   'encrypted, the server certificate is not checked. verify-full = checked '
   'against target_ssl_rootcert, host name included.'),
  ('target_ssl_rootcert', '',
   'CA file (a path on the QueryHub host) that verify-full checks server '
   'certificates against. One file may hold several CAs, one per cloud. Used '
   'only with a verifying mode.'),
  ('target_ssl_verify_hosts', '',
   'Host globs (comma or space separated) whose connections check the server '
   'certificate: verify-full for PostgreSQL, TrustServerCertificate=no for '
   'SQL Server. Example: *.rds.amazonaws.com'),
  ('target_ssl_verify_exempt_hosts', '',
   'Host globs that never check the server certificate, for a server whose '
   'certificate cannot be verified. Beats target_ssl_verify_hosts and a '
   'verifying target_ssl_mode.')
ON CONFLICT (key) DO NOTHING;
