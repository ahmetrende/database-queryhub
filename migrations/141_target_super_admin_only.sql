-- 141: a target that fleet-wide rights do not reach, except a super-admin's.
--
-- Some servers hold the control plane itself: QueryHub's own metadata
-- database, the DBA's tooling, the fleet inventory. A fleet-wide grant, a
-- fleet-wide waiver and a scoped admin role reach every target by definition,
-- so before this column they reached those servers too.
--
-- With the flag set, a principal who is not a super-admin reaches the target
-- only through a grant that names it. A grant written for one person on that
-- server is a deliberate exception, and it keeps working. The access model
-- (access.py, and teams.py when access_model_v2 is off) enforces the rule.
-- Only a super-admin can grant access to a flagged target.

ALTER TABLE target_servers
    ADD COLUMN IF NOT EXISTS super_admin_only BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN target_servers.super_admin_only IS
    'Fleet-wide grants, fleet-wide waivers and non-super admin roles do not '
    'reach this target. Only a super-admin, or a grant that names the target, '
    'does (migration 141).';
