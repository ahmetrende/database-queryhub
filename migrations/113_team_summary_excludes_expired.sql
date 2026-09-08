-- 113: `v_team_summary.grant_count` also stops counting EXPIRED grants.
--
-- 112 removed the revoked ones and stopped a line short. Migration 096 gave
-- team grants an `expires_at`, and a grant past it authorizes nothing — the
-- resolver has always filtered on it, and `test_every_grant_query_checks_expiry`
-- exists precisely because the rule has to hold in every reader rather than in
-- the famous one. That test caught the matching omission in the Slack detail
-- view; this is the view it could not see.
--
-- Two migrations for one view in a row is untidy and honest: the second rule
-- came from running the test suite, not from reading the first fix again.

CREATE OR REPLACE VIEW v_team_summary AS
SELECT t.id,
       t.name,
       t.description,
       (SELECT count(*) FROM team_members tm
         WHERE tm.team_id = t.id) AS member_count,
       (SELECT count(*) FROM team_target_grants g
         WHERE g.team_id = t.id
           AND g.revoked_at IS NULL
           AND (g.expires_at IS NULL OR g.expires_at > NOW())) AS grant_count,
       t.created_at
  FROM teams t
 ORDER BY t.name;

COMMENT ON VIEW v_team_summary IS
  'One row per team with live member and grant counts. `grant_count` excludes '
  'grants that are revoked or past their expiry — neither is access.';
