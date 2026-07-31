-- Ownership map: each RDS target -> the engineering pod/team that owns it.
--
-- The company keeps a service catalog (Backstage-style: services, their
-- backing databases, and the owning pod, plus per-pod leads and members).
-- That catalog has no host/endpoint identifiers, so a target is linked to a
-- pod by matching the target alias and its default database name against the
-- catalog's service and database-resource names. This table records the
-- result so the bot can answer "who owns this database" (routing a question
-- or an approval to the right pod lead) without re-deriving it every time.
--
-- A target can legitimately map to more than one pod (e.g. a write service in
-- one pod plus read-model projections in another); one row per (target, pod),
-- with is_primary flagging the main owner. `confidence` records how strong the
-- match signal was; `source` records what matched.
--
-- Rows carry real people/team/pod names, which never live in the repo (same
-- rule as mssql_host_map): the schema is here, the data is filled at runtime
-- from the external catalog and refreshed as it changes.
CREATE TABLE IF NOT EXISTS target_pod_owner (
  id               BIGSERIAL PRIMARY KEY,
  target_server_id INTEGER NOT NULL REFERENCES target_servers(id) ON DELETE CASCADE,
  pod_id           TEXT NOT NULL,
  pod_name         TEXT,
  division         TEXT,
  lead             TEXT,
  members          TEXT[],
  is_primary       BOOLEAN NOT NULL DEFAULT TRUE,
  confidence       TEXT NOT NULL DEFAULT 'high'
                     CHECK (confidence IN ('high', 'med', 'low')),
  source           TEXT,
  note             TEXT,
  matched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (target_server_id, pod_id)
);

CREATE INDEX IF NOT EXISTS idx_target_pod_owner_target
  ON target_pod_owner (target_server_id);
