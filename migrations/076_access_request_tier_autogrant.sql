-- Access requests: persist the requested tier as a real column.
--
-- Approving an access request now AUTO-CREATES the per-user grant from the
-- request's own fields (requester + target + database + tier) — see
-- access_requests.decide(). Until now the tier only rode inside the reason
-- text as a "[requested tier: RO]" prefix (web flow), which is display-only
-- and unparseable to trust. Store it properly; backfill what the prefix
-- carried. NULL = not stated (older rows / Slack modal) — the auto-grant
-- then defaults to the least-privilege tier (ro).

ALTER TABLE access_requests
    ADD COLUMN IF NOT EXISTS requested_tier TEXT
        CHECK (requested_tier IN ('ro', 'rw', 'ddl'));

COMMENT ON COLUMN access_requests.requested_tier IS
$$Tier the requester asked for (ro/rw/ddl). NULL = not stated; approval auto-grant defaults to ro. Web flow sets it explicitly; the reason text keeps its human-readable "[requested tier: X]" prefix for display.$$;

-- Backfill from the reason prefix where present.
UPDATE access_requests
   SET requested_tier = lower(substring(reason from '^\[requested tier: (RO|RW|DDL)\]'))
 WHERE requested_tier IS NULL
   AND reason ~ '^\[requested tier: (RO|RW|DDL)\]';
