-- Per-admin scope columns. NULL on a column = no restriction in that
-- dimension. An admin with all three NULL approves everything.

ALTER TABLE admins ADD COLUMN IF NOT EXISTS scope_team_ids   INTEGER[];
ALTER TABLE admins ADD COLUMN IF NOT EXISTS scope_target_ids INTEGER[];
ALTER TABLE admins ADD COLUMN IF NOT EXISTS max_tier         TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'admins_max_tier_check'
    ) THEN
        ALTER TABLE admins
            ADD CONSTRAINT admins_max_tier_check
            CHECK (max_tier IS NULL OR max_tier IN ('ro', 'rw', 'ddl'));
    END IF;
END $$;

COMMENT ON COLUMN admins.scope_team_ids IS
$$NULL = approve requests from any team. Array of team_id values = only requests from a requester who is a member of at least one of these teams. Requester with no team membership is NOT matched by a non-NULL scope.$$;

COMMENT ON COLUMN admins.scope_target_ids IS
$$NULL = approve requests on any target. Array of target_server_id values = only requests where the chosen target is in this list.$$;

COMMENT ON COLUMN admins.max_tier IS
$$NULL = approve any tier (ro/rw/ddl). 'ro' / 'rw' / 'ddl' = the highest tier this admin can approve. e.g. max_tier='rw' approves ro+rw, NOT ddl.$$;
