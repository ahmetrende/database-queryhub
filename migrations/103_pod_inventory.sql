-- Pod inventory: who is in each pod, and which database each service talks to.
--
-- The company is moving access off ad-hoc teams and onto pods, so the bot needs
-- its own copy of the pod picture. Four upstream systems each own one part of it
-- and none of them owns all of it:
--
--   * the people API   - pods, their members, and work e-mail   (changes daily)
--   * the internal developer portal - the pod captain, plus every service and the
--     database resources it depends on                          (daily sync job)
--   * the task board   - each pod's board, plan boards, open work
--   * the service catalog repository - organisation and product labels, which have
--     no live source at all and are therefore the oldest field here
--
-- The only key the four share is the pod's display name, so the load is done
-- outside the database and the result is written here as two flat snapshots.
-- Rows carry real people, e-mail addresses and production endpoints, so - same
-- rule as mssql_host_map and target_pod_owner - the schema lives in the repo and
-- the data is loaded at runtime and refreshed as it changes.
--
-- Both tables are full-refresh snapshots: a load deletes every row and re-inserts,
-- so `loaded_at` is the same for the whole set and tells you how stale it is.

-- One row per (pod, person).
CREATE TABLE IF NOT EXISTS pod_roster (
  id            BIGSERIAL PRIMARY KEY,
  pod_slug      TEXT,               -- portal slug, e.g. 'team-a'; empty if the pod is too new to appear there
  pod_name      TEXT NOT NULL,      -- the join key across all four sources
  org           TEXT,               -- service catalog, no live source
  product       TEXT,               -- service catalog, no live source
  kanban_board  TEXT,
  plan_boards   TEXT,               -- '|'-separated plan board names
  open_work     INTEGER,            -- open task count on the pod's own and other boards
  member_count  INTEGER,
  person_name   TEXT NOT NULL,
  lead_status   TEXT NOT NULL DEFAULT 'unknown'
                  CHECK (lead_status IN ('yes', 'no', 'unknown')),
  email         TEXT,
  loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pod_roster_pod   ON pod_roster (pod_slug);
CREATE INDEX IF NOT EXISTS idx_pod_roster_email ON pod_roster (lower(email));

-- One row per (service, database dependency, environment).
CREATE TABLE IF NOT EXISTS pod_service_database (
  id              BIGSERIAL PRIMARY KEY,
  pod_slug        TEXT,             -- empty when no source could name an owner
  service_name    TEXT NOT NULL,
  rds_endpoint    TEXT,             -- filled only when the dependency names a real RDS host
  pod_source      TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (pod_source IN ('idp', 'same_repo_guess', 'same_system_guess',
                                          'service_catalog_guess', 'unknown')),
  cloud           TEXT CHECK (cloud IN ('aws', 'huawei', '')),
  environment     TEXT,
  endpoint_status TEXT NOT NULL DEFAULT 'opaque'
                    CHECK (endpoint_status IN ('resolved', 'opaque')),
  resource_name   TEXT,             -- the dependency as the portal records it, host or logical name
  repo            TEXT,
  system_name     TEXT,
  loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pod_service_db_pod      ON pod_service_database (pod_slug);
CREATE INDEX IF NOT EXISTS idx_pod_service_db_endpoint ON pod_service_database (rds_endpoint)
  WHERE rds_endpoint IS NOT NULL AND rds_endpoint <> '';
