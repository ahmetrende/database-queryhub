-- Cross-team bypass: a flagged requester sees every enabled target
-- regardless of team grants, while still being a non-admin (cannot
-- approve/reject queries). Useful for senior devs / on-call DBAs who need
-- broad query access but should not be wielding the approve button.
--
-- Default FALSE — opt-in per user via UPDATE.

ALTER TABLE requesters
    ADD COLUMN IF NOT EXISTS bypass_team_grants BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN requesters.bypass_team_grants IS
$$When TRUE, the user's modal shows every enabled target (and every database) regardless of team_target_grants — same visibility an admin has, but without admin powers. Default FALSE.$$;
