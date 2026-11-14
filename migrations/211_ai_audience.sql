-- 211: who can use the AI assistant (2026-11-14).
--
-- Each row is one rule. `kind` says whom the rule names:
--   everyone   every signed-in user. `value` is empty.
--   person     one principal id (a Slack user id, or local:<name>).
--   team       one team id (team.id).
--   role       super_admin, dba, admin or approver.
-- A person can use the assistant when a rule names them. With no rule,
-- nobody can use it. Rules only open the assistant. No rule closes it.
--
-- The seed row opens the assistant to the super-admins only, so the first
-- calls are a pilot. Safe to run again.

CREATE TABLE IF NOT EXISTS ai_audience (
    id          serial PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('everyone', 'person', 'team', 'role')),
    value       text NOT NULL DEFAULT '',
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, value)
);

INSERT INTO ai_audience (kind, value, created_by)
VALUES ('role', 'super_admin', 'migration 211')
ON CONFLICT (kind, value) DO NOTHING;
