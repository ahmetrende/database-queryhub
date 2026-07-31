-- Time-bounded, tier-scoped auto-approval grants.
--
-- A user with an active grant skips the admin approval gate up to the
-- grant's max_tier. RW / DDL queries above the grant's tier fall back
-- to the normal pending → admin approval flow.
--
-- expires_at NULL = no expiry.
-- starts_at defaults to NOW() so a "just give me access starting today"
-- INSERT is a one-line statement.

CREATE TABLE IF NOT EXISTS auto_approve_grants (
    id            BIGSERIAL    PRIMARY KEY,
    slack_user_id TEXT         NOT NULL
                  CHECK (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'),
    max_tier      TEXT         NOT NULL
                  CHECK (max_tier IN ('ro', 'rw', 'ddl')),
    starts_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ,
    reason        TEXT,
    granted_by    TEXT,
    granted_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Sanity: if both bounds are set, expires_at must be after starts_at.
    CHECK (expires_at IS NULL OR expires_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_auto_approve_grants_user
    ON auto_approve_grants (slack_user_id, expires_at);

COMMENT ON TABLE auto_approve_grants IS
$$Per-user time-bounded auto-approval grants. A query whose required_mode is <= grant.max_tier and that runs while NOW() falls inside [starts_at, expires_at) skips admin approval and dispatches to the executor immediately. Multiple rows per user are allowed (e.g. RO until forever + RW only for a maintenance window) — the bot picks the most-permissive matching row at submission time.$$;

COMMENT ON COLUMN auto_approve_grants.max_tier IS
$$Highest tier this grant covers. 'ro' approves RO; 'rw' approves RO+RW; 'ddl' approves any tier. Queries above the grant's tier fall back to admin approval.$$;

COMMENT ON COLUMN auto_approve_grants.expires_at IS
$$NULL = never expires. Otherwise inclusive end; bot evaluates `expires_at > NOW()`. For scheduled queries, the bot ALSO checks the grant will still be valid at scheduled_for — otherwise it falls back to admin approval and warns the user.$$;

-- A small standalone view so /sql roles + admin views can render the
-- "currently auto-approved" badge without inlining the same predicate.
-- DROP-then-CREATE (not CREATE OR REPLACE) so this stays re-runnable after a
-- later migration widens the view: apply_migrations re-runs every file in
-- order, and CREATE OR REPLACE cannot shrink a view that 051 has widened.
DROP VIEW IF EXISTS v_active_auto_approve;
CREATE VIEW v_active_auto_approve AS
SELECT slack_user_id,
       max_tier,
       starts_at,
       expires_at,
       reason,
       granted_by,
       id AS grant_id
  FROM auto_approve_grants
 WHERE starts_at <= NOW()
   AND (expires_at IS NULL OR expires_at > NOW());

COMMENT ON VIEW v_active_auto_approve IS
$$One row per active auto-approve grant (NOW() inside [starts_at, expires_at)). Multiple rows per user possible — readers should pick the highest max_tier when summarising.$$;
