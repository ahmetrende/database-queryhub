-- Time-bounded admin role for vacation / on-call deputisation. Only
-- super-admins (a row in `admins` with all three scope columns NULL
-- and enabled = TRUE) can grant. Enforcement of "only super admins
-- grant" is in the application layer (we manage these rows via raw
-- SQL anyway, so no DB-side trigger — but the helper functions check
-- the rule before INSERT/UPDATE).
--
-- Design: keep the permanent `admins` table immutable. A temp grant
-- adds a row here; `is_admin` / `can_approve` consult both tables.
-- This preserves "who was admin on date Y" audit even after expiry.

CREATE TABLE IF NOT EXISTS temp_admin_grants (
    id                SERIAL       PRIMARY KEY,
    slack_user_id     TEXT         NOT NULL
                      CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'),
    -- Mirrors admins.* scope columns. NULL = wildcard on that dimension.
    max_tier          TEXT
                      CHECK (max_tier IN ('ro', 'rw', 'ddl')),
    scope_team_ids    INTEGER[],
    scope_target_ids  INTEGER[],
    starts_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at        TIMESTAMPTZ,
    reason            TEXT,
    granted_by        TEXT         NOT NULL,    -- super-admin's slack id
    granted_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    revoked_at        TIMESTAMPTZ,              -- early-revoke marker (NULL = active until expires_at)
    -- Sanity: if both bounds set, expires_at after starts_at.
    CHECK (expires_at IS NULL OR expires_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_temp_admin_grants_user_active
    ON temp_admin_grants (slack_user_id, expires_at);

COMMENT ON TABLE temp_admin_grants IS
$$Per-user, time-bounded admin role. `admins.is_admin()` consults both this table and the permanent admins table; a user is admin if NOW() falls inside [starts_at, expires_at) AND revoked_at IS NULL. Same scope columns as admins (max_tier / scope_team_ids / scope_target_ids), evaluated identically by can_approve().$$;

COMMENT ON COLUMN temp_admin_grants.max_tier IS
$$NULL = wildcard (approves any tier). 'ro' / 'rw' / 'ddl' narrow to that tier or lower. Same semantics as admins.max_tier.$$;

COMMENT ON COLUMN temp_admin_grants.scope_team_ids IS
$$NULL = any team. Non-NULL = requester must belong to at least one team in this list. Same semantics as admins.scope_team_ids.$$;

COMMENT ON COLUMN temp_admin_grants.scope_target_ids IS
$$NULL = any target. Non-NULL = request target must be in this list. Same semantics as admins.scope_target_ids.$$;

COMMENT ON COLUMN temp_admin_grants.expires_at IS
$$NULL = no auto-expiry (revoke manually via revoked_at = NOW()). Otherwise inclusive end; admin status disappears the moment NOW() >= expires_at.$$;

COMMENT ON COLUMN temp_admin_grants.granted_by IS
$$Slack id of the super-admin who issued this grant. Audit-only — the application layer enforces "only super-admins can INSERT here".$$;

COMMENT ON COLUMN temp_admin_grants.revoked_at IS
$$Set to NOW() to expire a grant before its scheduled expires_at. The row stays for audit; `v_active_temp_admins` filters revoked rows out.$$;

-- Active = inside [starts_at, expires_at) AND not revoked. Mirrors the
-- v_active_auto_approve pattern.
CREATE OR REPLACE VIEW v_active_temp_admins AS
SELECT slack_user_id,
       max_tier,
       scope_team_ids,
       scope_target_ids,
       starts_at,
       expires_at,
       reason,
       granted_by,
       id AS grant_id
  FROM temp_admin_grants
 WHERE starts_at  <= NOW()
   AND (expires_at IS NULL OR expires_at > NOW())
   AND revoked_at IS NULL;

COMMENT ON VIEW v_active_temp_admins IS
$$One row per currently-active temp admin grant. Multiple per user possible — the most-permissive row wins when evaluating can_approve.$$;
