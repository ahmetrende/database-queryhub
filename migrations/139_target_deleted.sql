-- A target whose instance no longer exists is DELETED, not merely disabled.
--
-- The hourly inventory sync disabled a target once v_server reported its
-- instance gone, and then it looked exactly like any other disabled target: the
-- admin screen offered to enable it, and it sat among the targets that are only
-- waiting for credentials. Measured 2026-09-30: 26 of 68 disabled targets were
-- instances that had been deleted, and every one of their endpoints had stopped
-- resolving in DNS.
--
--   deleted_at      When the instance was found gone: v_server's deleted_at when
--                   the inventory reports it, otherwise the moment it was marked.
--   deleted_reason  Why, in words the admin screen shows.
--
-- The row is kept, never removed: request history, grants and audit rows point
-- at it. The CHECK is what makes "cannot be enabled" true everywhere, including
-- a hand-written UPDATE, instead of only on the screens that remember to ask.

ALTER TABLE target_servers ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE target_servers ADD COLUMN IF NOT EXISTS deleted_reason text;

ALTER TABLE target_servers DROP CONSTRAINT IF EXISTS target_servers_deleted_not_enabled;
ALTER TABLE target_servers ADD CONSTRAINT target_servers_deleted_not_enabled
    CHECK (NOT (enabled AND deleted_at IS NOT NULL));
