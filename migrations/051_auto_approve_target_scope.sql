-- 051: target-scoped auto-approve grants
--
-- Lets a grant be narrowed to a single target (and optionally one
-- database) so the RO-burst "1-hour window" can be scoped to just the
-- cluster the user is working on, not every target they can reach. NULL
-- columns keep the legacy broad behaviour, so existing grants are
-- unaffected and the matching logic treats NULL as "covers everything".
ALTER TABLE auto_approve_grants
    ADD COLUMN IF NOT EXISTS target_server_id INT
        REFERENCES target_servers(id) ON DELETE CASCADE;
ALTER TABLE auto_approve_grants
    ADD COLUMN IF NOT EXISTS database_name TEXT;

COMMENT ON COLUMN auto_approve_grants.target_server_id IS
$$NULL = grant covers every target (legacy/broad). Non-NULL = only auto-approves requests against this target — used by the narrow per-target RO windows.$$;
COMMENT ON COLUMN auto_approve_grants.database_name IS
$$NULL = any database on the (scoped) target. Non-NULL = only this database. Ignored when target_server_id IS NULL.$$;

-- DROP-then-CREATE (apply_migrations re-runs every file each time; 030 also
-- DROP+CREATEs this view, and CREATE OR REPLACE can't reorder/shrink a view).
DROP VIEW IF EXISTS v_active_auto_approve;
CREATE VIEW v_active_auto_approve AS
SELECT slack_user_id,
       max_tier,
       starts_at,
       expires_at,
       reason,
       granted_by,
       id AS grant_id,
       target_server_id,
       database_name
  FROM auto_approve_grants
 WHERE starts_at <= NOW()
   AND (expires_at IS NULL OR expires_at > NOW());
