-- Track the Slack file ID returned from files_upload_v2 so the cleanup job
-- can call files.delete on expiry. NULL means: no file uploaded (read query
-- with no result, or the upload failed) or already deleted by cleanup.

ALTER TABLE requests ADD COLUMN IF NOT EXISTS slack_file_id TEXT;

-- Partial index speeds up the "find expired uploads" cleanup query.
CREATE INDEX IF NOT EXISTS idx_requests_pending_cleanup
    ON requests (completed_at)
    WHERE slack_file_id IS NOT NULL;
