-- 096 — standing grants can expire.
--
-- Until now they could not. `auto_approve_grants` has always had `expires_at`
-- and honours it; `user_target_grants` had only a manual `revoked_at` and
-- `team_target_grants` had neither, so access granted for one afternoon's
-- migration outlived the migration, the quarter, and in one case the employee.
-- 90 standing grants existed the day this shipped and not one of them could
-- end on its own.
--
-- The failure mode is not a breach, it is accumulation: nothing ever tells
-- anyone a grant stopped being needed, so the only thing that removes one is a
-- human who remembers. Privilege built up invisibly, and the audit trail showed
-- a grant being created and never showed it mattering again.
--
-- NULL means "no expiry", which is what every existing row gets — this migration
-- changes nobody's access. An expiry is opt-in per grant, and the resolution
-- path treats a past `expires_at` exactly as it treats `revoked_at`: the row
-- stops matching. Expiry is evaluated at RESOLUTION time, never by a sweep, for
-- the same reason every other authorization answer here is computed live: a
-- background job that has not run yet is a window where the answer is stale.
--
-- `team_target_grants` also gains `revoked_at`, which it never had. A team grant
-- could only be DELETED, so revoking one destroyed the record of it having
-- existed — the opposite of what an audit trail is for.

ALTER TABLE user_target_grants
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

ALTER TABLE team_target_grants
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;

COMMENT ON COLUMN user_target_grants.expires_at IS
    'When this grant stops applying. NULL = never. Enforced at resolution time '
    'in teams.effective_grant_for_user, not by a sweep.';
COMMENT ON COLUMN team_target_grants.expires_at IS
    'When this grant stops applying. NULL = never. Enforced at resolution time.';
COMMENT ON COLUMN team_target_grants.revoked_at IS
    'Soft revoke, so a withdrawn team grant leaves a record instead of a hole.';

-- Partial indexes over the live rows only. Both tables are small (90 rows), so
-- this is about keeping the resolution query's shape honest as the fleet grows,
-- not about today's plan.
CREATE INDEX IF NOT EXISTS idx_utg_live
    ON user_target_grants (slack_user_id, target_server_id)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ttg_live
    ON team_target_grants (team_id, target_server_id)
    WHERE revoked_at IS NULL;

-- The auth-event triggers already fire on these tables (migration 060), so a
-- grant given an expiry DMs the affected user like any other change. An expiry
-- ARRIVING is a change worth telling someone about; an expiry PASSING is not an
-- UPDATE and fires nothing, which is why the notice below matters.
