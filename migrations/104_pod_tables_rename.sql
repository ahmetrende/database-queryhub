-- Rename and normalise the pod inventory, and retire the guessed ownership map.
--
-- Three changes, all naming and shape - no new facts:
--
--  1. `target_pod_owner` is dropped. It answered "which pod owns this database"
--     by matching a target's alias and default database name against service
--     names in a catalog export, which is why every row carried a `confidence`
--     column. `pod_mapping` now answers the same question from the real RDS
--     hostname the service actually connects to, so the guess has no readers
--     left. Its rows were already deleted; this drops the empty table.
--
--  2. `pod_roster` repeated every pod-level field (org, product, board, open
--     work) on each of its members' rows. It splits into `pod`, one row per
--     pod, and `pod_detail`, one row per person.
--
--  3. `pod_service_database` becomes `pod_mapping`.
--
-- All three tables stay full-refresh snapshots: a load deletes every row and
-- re-inserts, so `loaded_at` is uniform across a set and says how stale it is.
-- Rows carry real people, e-mail addresses and production endpoints, so the
-- schema lives here and the data is loaded at runtime.

DROP TABLE IF EXISTS target_pod_owner;

-- One row per pod. `name` is the key, not `slug`: the pod's display name is the
-- only identifier the four upstream sources share, and a pod created today may
-- not have reached the developer portal yet, so its slug can still be missing.
CREATE TABLE IF NOT EXISTS pod (
  id           BIGSERIAL PRIMARY KEY,
  slug         TEXT UNIQUE,        -- portal slug, e.g. 'team-a'
  name         TEXT NOT NULL UNIQUE,
  org          TEXT,               -- service catalog, no live source
  product      TEXT,               -- service catalog, no live source
  kanban_board TEXT,
  plan_boards  TEXT,               -- '|'-separated plan board names
  open_work    INTEGER,
  member_count INTEGER,
  lead_name    TEXT,               -- the captain; NULL when the portal names none
  loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (pod, person).
CREATE TABLE IF NOT EXISTS pod_detail (
  id          BIGSERIAL PRIMARY KEY,
  pod_name    TEXT NOT NULL REFERENCES pod (name) ON DELETE CASCADE,
  pod_slug    TEXT,
  person_name TEXT NOT NULL,
  email       TEXT,
  lead_status TEXT NOT NULL DEFAULT 'unknown'
                CHECK (lead_status IN ('yes', 'no', 'unknown')),
  loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pod_detail_pod   ON pod_detail (pod_slug);
CREATE INDEX IF NOT EXISTS idx_pod_detail_email ON pod_detail (lower(email));

-- One row per (service, database dependency, environment). No foreign key to
-- `pod`: the portal owns a couple of pods that carry services but no people, so
-- a slug here can legitimately have no row in `pod`.
CREATE TABLE IF NOT EXISTS pod_mapping (
  id              BIGSERIAL PRIMARY KEY,
  pod_slug        TEXT,            -- NULL when no source could name an owner
  service_name    TEXT NOT NULL,
  rds_endpoint    TEXT,            -- set only when the dependency names a real RDS host
  pod_source      TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (pod_source IN ('idp', 'same_repo_guess', 'same_system_guess',
                                          'service_catalog_guess', 'unknown')),
  cloud           TEXT CHECK (cloud IN ('aws', 'huawei')),
  environment     TEXT,
  endpoint_status TEXT NOT NULL DEFAULT 'opaque'
                    CHECK (endpoint_status IN ('resolved', 'opaque')),
  resource_name   TEXT,            -- the dependency as the portal records it
  repo            TEXT,
  system_name     TEXT,
  loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pod_mapping_pod      ON pod_mapping (pod_slug);
CREATE INDEX IF NOT EXISTS idx_pod_mapping_endpoint ON pod_mapping (rds_endpoint)
  WHERE rds_endpoint IS NOT NULL;

-- Carry the snapshot over from the previous shape, then drop it.
DO $$
BEGIN
  IF to_regclass('public.pod_roster') IS NOT NULL THEN
    INSERT INTO pod (slug, name, org, product, kanban_board, plan_boards,
                     open_work, member_count, lead_name, loaded_at)
    SELECT DISTINCT ON (r.pod_name)
           nullif(r.pod_slug, ''), r.pod_name, nullif(r.org, ''), nullif(r.product, ''),
           nullif(r.kanban_board, ''), nullif(r.plan_boards, ''),
           r.open_work, r.member_count,
           (SELECT l.person_name FROM pod_roster l
             WHERE l.pod_name = r.pod_name AND l.lead_status = 'yes' LIMIT 1),
           r.loaded_at
      FROM pod_roster r
     ORDER BY r.pod_name
    ON CONFLICT (name) DO NOTHING;

    INSERT INTO pod_detail (pod_name, pod_slug, person_name, email, lead_status, loaded_at)
    SELECT r.pod_name, nullif(r.pod_slug, ''), r.person_name,
           nullif(r.email, ''), r.lead_status, r.loaded_at
      FROM pod_roster r;
  END IF;

  IF to_regclass('public.pod_service_database') IS NOT NULL THEN
    INSERT INTO pod_mapping (pod_slug, service_name, rds_endpoint, pod_source, cloud,
                             environment, endpoint_status, resource_name, repo,
                             system_name, loaded_at)
    SELECT nullif(s.pod_slug, ''), s.service_name, s.rds_endpoint, s.pod_source,
           nullif(s.cloud, ''), s.environment, s.endpoint_status, s.resource_name,
           s.repo, s.system_name, s.loaded_at
      FROM pod_service_database s;
  END IF;
END $$;

DROP TABLE IF EXISTS pod_roster;
DROP TABLE IF EXISTS pod_service_database;
