-- 112: `v_team_summary.grant_count` stops counting revoked grants.
--
-- The view counted every row in `team_target_grants`, revoked or not, and the
-- Slack detail view (`/sql teams <name>`) listed them the same way. Latent
-- rather than live: production has zero revoked team grants today, which is
-- exactly why nobody noticed.
--
-- It stops being latent the first time a team grant is revoked — the team then
-- reads, to everyone who runs `/sql teams`, as still holding access it does
-- not have. On a screen whose whole job is to say who can reach what, a count
-- that includes tombstones is the wrong number in the dangerous direction.
--
-- `member_count` needs no filter: `team_members` has no revoked concept, a
-- removal is a DELETE.

CREATE OR REPLACE VIEW v_team_summary AS
SELECT t.id,
       t.name,
       t.description,
       (SELECT count(*) FROM team_members tm
         WHERE tm.team_id = t.id) AS member_count,
       (SELECT count(*) FROM team_target_grants g
         WHERE g.team_id = t.id AND g.revoked_at IS NULL) AS grant_count,
       t.created_at
  FROM teams t
 ORDER BY t.name;

COMMENT ON VIEW v_team_summary IS
  'One row per team with live member and grant counts. `grant_count` excludes '
  'revoked grants — a tombstone is not access.';
