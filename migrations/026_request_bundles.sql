-- Batch submission ("bundle") support.
--
-- A user can open `/sql batch` (sub-command, gated by bot_config.batch_enabled)
-- to submit up to N items (target / database / query) in one go. Each item
-- becomes its own `requests` row, linked by `bundle_id`. The existing per-item
-- approve / reject handlers keep working unchanged — only the notification
-- layer learns to group items under a single admin DM (PR1) and produce a
-- single summary DM to the requester once the bundle is fully decided (PR2).
--
-- Single-shot requests (legacy `/sql` modal) keep bundle_id = NULL and behave
-- exactly as before.

-- 1. Bundle status enum -----------------------------------------------------
--
-- pending   = at least one item still awaiting an admin decision
-- partial   = every item decided OR cancelled, but not all approved
--             (mixed approve / reject — informational only)
-- decided   = every item reached a terminal state
-- cancelled = the entire bundle was cancelled (requester or admin)
--
-- We don't drive scheduling off this enum — per-item `requests.status` is the
-- source of truth for the scheduler. Bundle status is a UI rollup.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'bundle_status') THEN
        CREATE TYPE bundle_status AS ENUM (
            'pending',
            'partial',
            'decided',
            'cancelled'
        );
    END IF;
END$$;

-- 2. request_bundles --------------------------------------------------------

CREATE TABLE IF NOT EXISTS request_bundles (
    id                  BIGSERIAL    PRIMARY KEY,
    requester_slack_id  TEXT         NOT NULL,
    requester_name      TEXT,
    justification       TEXT,
    scheduled_for       TIMESTAMPTZ,
    status              bundle_status NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_request_bundles_requester
    ON request_bundles (requester_slack_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_request_bundles_pending
    ON request_bundles (status)
    WHERE status IN ('pending');

COMMENT ON TABLE request_bundles IS
$$Multi-query batch submission. One row per `/sql batch` submission; each item lives in `requests` with bundle_id pointing here. status is a rollup of the per-item request statuses (pending until all decided, then decided / partial / cancelled). Single-shot /sql submissions do NOT create a row here.$$;

COMMENT ON COLUMN request_bundles.justification IS
$$Bundle-level justification text (single field across all items in the batch).$$;

COMMENT ON COLUMN request_bundles.scheduled_for IS
$$Bundle-level scheduled run-time. NULL = run each item as soon as its admin approval lands. If set, items move to `scheduled` on approval and the existing per-request scheduler picks them up.$$;

-- 3. Link columns on requests ----------------------------------------------

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS bundle_id BIGINT
        REFERENCES request_bundles(id) ON DELETE SET NULL;

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS position INT;

CREATE INDEX IF NOT EXISTS idx_requests_bundle
    ON requests (bundle_id, position)
    WHERE bundle_id IS NOT NULL;

COMMENT ON COLUMN requests.bundle_id IS
$$Parent bundle (NULL for legacy single-shot /sql submissions). A bundle's items share requester / justification / scheduled_for but otherwise behave like independent requests — including audit_log, executor, and per-item approve / reject handlers.$$;

COMMENT ON COLUMN requests.position IS
$$1-based position of this item inside its bundle. Used purely for display ordering in admin DMs and result summaries.$$;

-- 4. Bundle-aware notification tracking ------------------------------------
--
-- For single-shot requests the existing row {request_id, admin, channel, ts}
-- shape stays as-is. For a bundle, each admin gets ONE DM that covers every
-- item — we use a separate sentinel row per (bundle, admin) with request_id
-- NULL and bundle_id set, so chat.update can address that DM without joining
-- through any specific item.

ALTER TABLE request_notifications
    ADD COLUMN IF NOT EXISTS bundle_id BIGINT
        REFERENCES request_bundles(id) ON DELETE CASCADE;

-- The original UNIQUE (request_id, admin_slack_id) constraint is fine for
-- single requests but blocks bundle rows (request_id is NULL). Drop and
-- re-create as a partial-unique pair: one constraint for per-request rows,
-- one for per-bundle rows.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'request_notifications_request_id_admin_slack_id_key'
    ) THEN
        ALTER TABLE request_notifications
            DROP CONSTRAINT request_notifications_request_id_admin_slack_id_key;
    END IF;
END$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_request_notifications_request
    ON request_notifications (request_id, admin_slack_id)
    WHERE request_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_request_notifications_bundle
    ON request_notifications (bundle_id, admin_slack_id)
    WHERE bundle_id IS NOT NULL;

-- Either request_id or bundle_id must be set (never both, never neither).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'request_notifications_one_target'
    ) THEN
        ALTER TABLE request_notifications
            ADD CONSTRAINT request_notifications_one_target
            CHECK (
                (request_id IS NOT NULL AND bundle_id IS NULL)
                OR
                (request_id IS NULL AND bundle_id IS NOT NULL)
            );
    END IF;
END$$;

COMMENT ON COLUMN request_notifications.bundle_id IS
$$Set when this row tracks the per-(bundle, admin) DM that carries one block per item. For single-shot requests this stays NULL and request_id is set instead. Mutually exclusive with request_id (CHECK constraint).$$;

-- 5. Feature flag ----------------------------------------------------------

INSERT INTO bot_config (key, value, description) VALUES
    ('batch_enabled', 'off',
     'When "on", users can run `/sql batch` to submit up to 5 queries in one modal. When "off" (default), the sub-command is hidden and rejects invocations. Single-shot `/sql` is unaffected either way.')
ON CONFLICT (key) DO NOTHING;

INSERT INTO bot_config (key, value, description) VALUES
    ('batch_max_items', '5',
     'Maximum items per /sql batch submission. Slack modal view has a 100-block hard limit; ~5 items keeps the modal readable. Items above this are rejected at submit time with a per-field error.')
ON CONFLICT (key) DO NOTHING;
