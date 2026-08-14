-- `requests.unmasked`: the requester asked to see this result without masking.
--
-- INTENT, not authority. Whether the request may actually run unmasked is
-- re-derived at execution from `admins.is_super_admin(requester)`, so a request
-- submitted by a super-admin who has since lost that standing runs MASKED. The
-- column records what was asked for; the answer is recomputed every time.
--
-- Default false, so every existing row and every ordinary submission is
-- unaffected. The submit path refuses the flag outright for anyone who is not a
-- super-admin rather than silently ignoring it — a client sending it is either a
-- bug or an attempt, and both deserve to be loud.
--
-- Each unmasked EXECUTION also writes its own audit_log row (action
-- `result_unmasked`), because "everything is logged" has to include the moment
-- masking was skipped.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS unmasked boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN requests.unmasked IS
    'Requester asked for an unmasked result. Intent only — the executor '
    're-checks super-admin standing at run time and masks anyway if it is gone.';
