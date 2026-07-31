-- Bot-DB IP map for SQL Server Availability Group nodes.
--
-- An AG's read-only routing redirects a read-intent connection to a replica's
-- routing URL, which is a hostname/FQDN (e.g. tcp://NODE.domain:1433). The bot
-- host usually can't resolve those names (no DNS for that subnet), and putting
-- them in /etc/hosts is a per-machine dependency that's easy to forget on a
-- rebuild/migration. Instead the bot resolves the routing target itself: for a
-- read-only query it discovers the current readable-secondary's server name
-- from the AG DMVs, maps that name to a bot-reachable IP HERE, and connects
-- straight to that IP (so the driver's own FQDN redirect is never used).
--
-- One row per AG replica: server_name is the name as it appears in
-- sys.dm_hadr_availability_replica_states / sys.availability_replicas
-- (typically the short NetBIOS name). Rows are filled at runtime (they carry
-- real IPs, which never live in the repo).
CREATE TABLE IF NOT EXISTS mssql_host_map (
  server_name TEXT PRIMARY KEY,
  ip          TEXT NOT NULL,
  note        TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
