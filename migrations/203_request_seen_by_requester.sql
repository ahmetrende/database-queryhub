-- 203: when the requester first saw a request after it ended.
--
-- The workspace in the portal keeps a request from Claude in Current session
-- until it ended and its requester opened it. A request ends when it ran,
-- failed, or closed without a run. The portal calls
-- `POST /api/queries/<id>/seen` when the requester opens such a request, and
-- that call writes this time once. `GET /api/queries/<id>` only reads it. A
-- read by Claude through the MCP door writes nothing. Safe to run again.
--
-- The removed stdio MCP server also wrote requests with origin 'mcp'. Nobody
-- can open those in the portal as unread, so the first run marks the ended
-- ones as seen. A second run adds no column and marks nothing, so it never
-- hides a new request that the requester has not opened. The status list is
-- `mapping.ENDED_STATUSES`, compared as text: it names 'expired', which the
-- request_status type does not have.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = current_schema()
                    AND table_name = 'requests'
                    AND column_name = 'requester_seen_at') THEN
    ALTER TABLE requests ADD COLUMN requester_seen_at TIMESTAMPTZ;
    UPDATE requests SET requester_seen_at = NOW()
     WHERE origin = 'mcp'
       AND status::text IN ('completed', 'failed', 'rejected', 'changes_requested',
                            'cancelled', 'expired');
  END IF;
END $$;

COMMENT ON COLUMN requests.requester_seen_at IS
$$When the requester first opened this request in the portal after it ended (POST /api/queries/<id>/seen). NULL until the request ends and the requester opens it. Reads by Claude through the MCP door do not count.$$;
