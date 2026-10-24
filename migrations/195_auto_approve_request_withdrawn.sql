-- A requester can withdraw their own pending auto-approve window request
-- (POST /api/auto-approve-requests/{id}/withdraw).
--
-- The status CHECK of migration 052 allows pending, approved and rejected. A
-- withdrawal is none of them. If QueryHub stored it as `rejected`, a request
-- that its own requester closed would look like a refusal by an admin. So the
-- CHECK gets a fourth value.
--
-- The partial unique index uq_auto_approve_requests_pending covers rows with
-- status = 'pending' only. A withdrawn row leaves it, so the requester can ask
-- for the same window again.

ALTER TABLE auto_approve_requests DROP CONSTRAINT IF EXISTS auto_approve_requests_status_check;
ALTER TABLE auto_approve_requests ADD CONSTRAINT auto_approve_requests_status_check
    CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn'));
