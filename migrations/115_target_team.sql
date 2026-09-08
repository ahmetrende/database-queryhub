-- 115: which team owns a target, as a relation rather than a runtime join.
--
-- A target has been an independent object: nothing in the model says who owns
-- it. The pod work needed that link and got it by joining a hostname string
-- at query time (`lower(target_servers.host) = lower(pod_mapping.rds_endpoint)`),
-- which is a join no screen can show, no API can return, and nobody can
-- correct by hand when the portal is wrong or silent — and it is silent for
-- 11 of the 43 enabled targets.
--
-- MANY-TO-MANY, and the data settles it rather than taste. Six production
-- databases are already claimed by TWO pods each, because services owned by
-- different pods share one RDS. A nullable `target_servers.owner_team_id`
-- would have forced a choice between them, silently, on the row that decides
-- who may approve access to that database.
--
-- Ownership is NOT access. A row here grants nothing and never will: it says
-- who is responsible for a database, which is what lets a team's lead approve
-- their own team's requests to it. What anyone may READ stays in
-- `access_grant`, decided by a person.
--
-- Plain rows rather than the soft-delete the nine-table model uses elsewhere.
-- A revoked grant is evidence — somebody may be reading a message about it —
-- and ownership is not: the audit question "why could they approve this in
-- March" is answered by `role_assignment.revoked_at`, which does keep its
-- history. Current state is enough here.

CREATE TABLE IF NOT EXISTS target_team (
  target_id   BIGINT      NOT NULL REFERENCES target_servers (id) ON DELETE CASCADE,
  team_id     BIGINT      NOT NULL REFERENCES team (id)           ON DELETE RESTRICT,
  -- Which sync owns this row, or NULL when a person linked it by hand. Same
  -- rule as team.source and role_assignment.source: own your rows, leave
  -- everyone else's alone. This is the half that makes "not mandatory, but
  -- possible" true — a target the portal has never heard of can still be
  -- linked, and no sync will take it away.
  source      TEXT,
  created_by  BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (target_id, team_id)
);

-- ON DELETE CASCADE on the target and RESTRICT on the team, deliberately
-- asymmetric: ownership is metadata about a target, so retiring the target
-- takes it with it, while a team that still owns something is not a team to
-- remove without noticing.

COMMENT ON TABLE target_team IS
  'Which team is responsible for a target. Many-to-many: six production '
  'databases are shared between two pods. Grants nothing — it is what lets a '
  'team lead approve their own team''s requests to the databases their team '
  'owns (see scripts/sync_team_approvers.py).';

CREATE INDEX IF NOT EXISTS idx_target_team_team ON target_team (team_id);
CREATE INDEX IF NOT EXISTS idx_target_team_source
    ON target_team (source) WHERE source IS NOT NULL;
