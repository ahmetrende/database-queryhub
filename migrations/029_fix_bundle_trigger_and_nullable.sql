-- Two bundle-fence bugs surfaced in production after PR2.
--
-- 1) request_notifications.request_id was still NOT NULL — migration
--    026 dropped the old UNIQUE and added a CHECK that allows
--    (request_id IS NULL AND bundle_id IS NOT NULL), but never
--    relaxed the column itself. notify_admins_bundle therefore failed
--    to record DM coordinates with a NotNullViolation, and the bot
--    later had no way to chat.update those bundle DMs.
--
-- 2) Bundle status trigger raced when two items reached terminal
--    states concurrently. Each AFTER UPDATE trigger SELECTed from
--    requests under its own statement snapshot, and could see the
--    sibling item as still 'executing' (the sibling's transaction
--    hadn't committed yet), so each trigger fire computed
--    new_status='pending' and the bundle stayed pending forever.
--    Fix: pg_advisory_xact_lock(bundle_id) at the start of the
--    trigger serialises concurrent triggers for the same bundle,
--    so the second one always observes the first's committed state.

-- ------------------------------------------------------------------
-- 1. Drop NOT NULL on request_notifications.request_id
-- ------------------------------------------------------------------

ALTER TABLE request_notifications
    ALTER COLUMN request_id DROP NOT NULL;

-- ------------------------------------------------------------------
-- 2. Race-safe trigger
-- ------------------------------------------------------------------

CREATE OR REPLACE FUNCTION trg_recompute_bundle_status() RETURNS trigger AS $$
DECLARE
    bid              BIGINT;
    in_flight        INT;
    n_items          INT;
    all_cancelled    BOOL;
    has_completed    BOOL;
    has_terminal_neg BOOL;
    new_status       bundle_status;
BEGIN
    IF NEW.bundle_id IS NULL AND OLD.bundle_id IS NULL THEN
        RETURN NEW;
    END IF;
    bid := COALESCE(NEW.bundle_id, OLD.bundle_id);

    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NEW;
    END IF;

    -- Serialise concurrent recomputes for the SAME bundle so the
    -- second trigger always sees the first transaction's committed
    -- state. Released automatically at txn commit / rollback.
    PERFORM pg_advisory_xact_lock(bid);

    SELECT
        count(*),
        count(*) FILTER (WHERE status IN ('pending','approved','scheduled',
                                          'executing','awaiting_dba_manual',
                                          'changes_requested')),
        bool_and(status = 'cancelled'),
        bool_or(status = 'completed'),
        bool_or(status IN ('rejected','failed','cancelled'))
      INTO n_items, in_flight, all_cancelled, has_completed, has_terminal_neg
      FROM requests
     WHERE bundle_id = bid;

    IF n_items = 0 THEN
        RETURN NEW;
    END IF;

    IF in_flight > 0 THEN
        new_status := 'pending';
    ELSIF all_cancelled THEN
        new_status := 'cancelled';
    ELSIF has_completed AND has_terminal_neg THEN
        new_status := 'partial';
    ELSE
        new_status := 'decided';
    END IF;

    UPDATE request_bundles
       SET status = new_status
     WHERE id = bid AND status <> new_status;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger registration unchanged from 027; CREATE OR REPLACE above
-- swaps in the new body. (Re-issuing the DROP TRIGGER + CREATE TRIGGER
-- here just to make the migration self-contained.)
DROP TRIGGER IF EXISTS requests_recompute_bundle_status ON requests;
CREATE TRIGGER requests_recompute_bundle_status
    AFTER UPDATE OF status ON requests
    FOR EACH ROW
    EXECUTE FUNCTION trg_recompute_bundle_status();
