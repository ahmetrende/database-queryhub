-- Let the expiry scanner warn about grants that live in the nine-table model.
--
-- `grant_expiry_notices.grant_kind` only allowed 'user' and 'team', the two
-- legacy tables. Pod grants are written straight to `access_grant` and have no
-- legacy row to hang on, so the scanner could not have warned about one even if
-- it had looked — the ledger that stops a warning being sent twice would have
-- refused the row.
--
-- Latent when written: no native `access_grant` row carries an expiry today.
-- That is exactly why it is worth closing now — the first one to get an expiry
-- date would have lapsed in silence, and nobody would have been looking.
ALTER TABLE grant_expiry_notices
    DROP CONSTRAINT IF EXISTS grant_expiry_notices_grant_kind_check;

ALTER TABLE grant_expiry_notices
    ADD CONSTRAINT grant_expiry_notices_grant_kind_check
    CHECK (grant_kind IN ('user', 'team', 'access'));
