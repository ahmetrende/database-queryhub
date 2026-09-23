-- Read replicas: a read-only request runs on a healthy replica of its target.
--
-- A replica is its own row in target_servers, because it has its own host and
-- its own on/off switch -- but it is never a connection anyone picks. Every
-- list shows the primary's one name; `replica_of` says whose replica a row is,
-- and the executor decides at run time, per request, whether the replica
-- serves it (replicas.py). The inventory knows the relation (`v_server.
-- replica_source`), and import_targets_from_inventory.py links it.
--
-- A replica needs no credential of its own: a physical replica has the
-- primary's roles and passwords, so the primary's read-only login is used.
ALTER TABLE target_servers ADD COLUMN IF NOT EXISTS replica_of INTEGER
    REFERENCES target_servers(id) ON DELETE SET NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'target_servers_replica_not_self') THEN
        ALTER TABLE target_servers ADD CONSTRAINT target_servers_replica_not_self
            CHECK (replica_of IS NULL OR replica_of <> id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_target_servers_replica_of
    ON target_servers (replica_of) WHERE replica_of IS NOT NULL;

COMMENT ON COLUMN target_servers.replica_of IS
  $$The primary this row is a read replica of. Such a row is hidden from every picker; when enabled and healthy it serves the primary's read-only requests.$$;

-- Where a request actually ran, when that was not its own target. The request
-- stays ON the primary -- grants, approvals and history all name it -- so the
-- one fact the row would otherwise lose is which server did the work. The
-- cancel path needs it too: a backend pid means nothing on the wrong host.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS executed_target_id INTEGER
    REFERENCES target_servers(id) ON DELETE SET NULL;

COMMENT ON COLUMN requests.executed_target_id IS
  $$The read replica that ran this request, when it did not run on target_server_id. NULL = ran on its own target.$$;

INSERT INTO bot_config (key, value, description) VALUES
  ('replica_routing', 'off',
   'on = a read-only request runs on a healthy, enabled read replica of its target.'),
  ('replica_max_lag_seconds', '10',
   'A replica further behind its primary than this is not used.'),
  ('replica_health_ttl_seconds', '15',
   'How long one replica health check is trusted, per process.'),
  ('replica_read_your_writes_minutes', '5',
   'After a requester''s own RW/DDL run on a target, their reads stay on the primary this long. 0 = off.')
ON CONFLICT (key) DO NOTHING;
