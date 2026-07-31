-- Centralise bundle-status recompute as an AFTER UPDATE trigger on
-- requests.status. The application code (handlers + executor) only has
-- to refresh the admin DMs after a state change; the bundle.status
-- column stays in sync automatically.
--
-- Mirrors bundles.recompute_status() in Python — but lives in the DB so
-- every code path that mutates `requests.status` benefits without an
-- explicit call. (The Python helper stays for use cases that already
-- hold a cursor and want the new value back.)

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
    -- Skip when the row never belonged to a bundle on either side.
    IF NEW.bundle_id IS NULL AND OLD.bundle_id IS NULL THEN
        RETURN NEW;
    END IF;
    bid := COALESCE(NEW.bundle_id, OLD.bundle_id);

    -- Status didn't actually change — nothing to do.
    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NEW;
    END IF;

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
        -- Nothing left in the bundle (shouldn't happen with FK SET NULL,
        -- but be safe).
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

DROP TRIGGER IF EXISTS requests_recompute_bundle_status ON requests;
CREATE TRIGGER requests_recompute_bundle_status
    AFTER UPDATE OF status ON requests
    FOR EACH ROW
    EXECUTE FUNCTION trg_recompute_bundle_status();

COMMENT ON FUNCTION trg_recompute_bundle_status() IS
$$Keeps request_bundles.status in sync with the live mix of per-item statuses. Fired AFTER UPDATE OF status on requests. Rules match bundles.recompute_status() in Python: any in-flight item → pending; all cancelled → cancelled; mix of completed + terminal negatives → partial; else → decided.$$;
