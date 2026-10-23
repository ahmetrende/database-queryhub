-- A requester can withdraw their own pending access request
-- (POST /api/endpoint-requests/{id}/withdraw).
--
-- The status CHECK of migration 005 allows pending, approved and rejected. A
-- withdrawal is none of them. If QueryHub stored it as `rejected`, a request
-- that its own requester closed would look like a refusal by an admin. The
-- `outcome` field exists because SQL requests have that problem. So the CHECK
-- gets a fourth value.
--
-- The unique index of migration 005 covers rows with status = 'pending' only.
-- A withdrawn row leaves it, so the requester can ask for the same access
-- again.

ALTER TABLE access_requests DROP CONSTRAINT IF EXISTS access_requests_status_check;
ALTER TABLE access_requests ADD CONSTRAINT access_requests_status_check
    CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn'));

COMMENT ON COLUMN access_requests.status IS
    'pending → approved | rejected (an admin decides) | withdrawn (the requester withdraws it). CHECK-constrained.';
