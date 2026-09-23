-- Keep a password out of request history for the whole life of the request.
--
-- Migration 100 masked `requests.query` when a request reached a terminal
-- state. Until then the password sat there in cleartext: seconds for an
-- auto-approved run, days for a request waiting on an approver or on a DBA to
-- run it by hand, and it was deliberately kept for the manual case. The
-- operator's rule since 2026-09-23: a password is not stored at all.
--
-- The application now stores the statement MASKED from the first write and
-- keeps the original, encrypted with the master key, in `query_secret`, which
-- the executor alone reads (query_secrets.py). This migration adds the column
-- and makes the trigger:
--   * mask `query` on every INSERT and UPDATE -- a backstop for any write
--     path that forgets, so none of them can store a cleartext password;
--   * drop `query_secret` as soon as the request can no longer run.
-- `awaiting_dba_manual` is one of those now: the DBA sees the masked script
-- and sets a new password, rather than the old one waiting in the table.

ALTER TABLE requests ADD COLUMN IF NOT EXISTS query_secret text;

COMMENT ON COLUMN requests.query_secret IS
  'The submitted statement unmasked, Fernet-encrypted with the master key. '
  'Present only while the request can still run; the trigger drops it when '
  'the request ends. `query` always holds the masked text.';

-- Also covers '' inside a literal and the E'' form, which migration 100's
-- pattern cut short: `'it''s'` masked only `'it'` and left `'s'` in place.
CREATE OR REPLACE FUNCTION scrub_query_secrets(sql text) RETURNS text AS $$
  SELECT regexp_replace(
           COALESCE(sql, ''),
           '(\m(?:ENCRYPTED\s+)?PASSWORD\s*=?\s*)(E''(?:[^''\\]|\\.|'''')*''|N?''(?:[^'']|'''')*'')',
           '\1''***REDACTED***''',
           'gi');
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION requests_scrub_secrets() RETURNS trigger AS $$
BEGIN
  -- Idempotent: the marker is itself a literal, and masking it again
  -- replaces it with the same text.
  IF NEW.query ~* '(?:encrypted\s+)?password\s*=?\s*[EN]?''' THEN
    NEW.query := scrub_query_secrets(NEW.query);
  END IF;
  IF NEW.status IN ('completed', 'failed', 'cancelled', 'rejected', 'expired',
                    'changes_requested', 'awaiting_dba_manual') THEN
    NEW.query_secret := NULL;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_requests_scrub_secrets ON requests;
CREATE TRIGGER trg_requests_scrub_secrets
  BEFORE INSERT OR UPDATE ON requests
  FOR EACH ROW
  EXECUTE FUNCTION requests_scrub_secrets();

-- Anything still holding a cleartext literal, whatever its state. There is
-- no secret to keep for these rows: one that could still run will be refused
-- by the executor and has to be resubmitted with its password.
UPDATE requests
   SET query = scrub_query_secrets(query)
 WHERE query ~* '(?:encrypted\s+)?password\s*=?\s*[EN]?'''
   AND query IS DISTINCT FROM scrub_query_secrets(query);
