-- Stop cleartext passwords living in request history.
--
-- A role-creation script carries the new password as a literal, and the whole
-- statement is stored in `requests.query` so the request can be reviewed,
-- audited and re-run. Four such requests sat in the table for up to twelve
-- days (4116, 4132, 5096, 5097): the accounts were live, so anyone who could
-- read that column had a working login. They were masked by hand on
-- 2026-08-25 (audit_log 19548); this makes it automatic.
--
-- Why a trigger and not application code: nineteen places across five modules
-- write a terminal status, two separate processes run the executor, and an
-- operator can UPDATE the row from psql. A trigger is the one point every
-- writer goes through.
--
-- The masking happens when the request REACHES A TERMINAL STATE, not at
-- submit: the executor has to send the real statement to the server, and an
-- approver has to see what they are approving. `awaiting_dba_manual` is
-- deliberately NOT terminal here — that status means a human still has to run
-- the statement by hand, and masking it would destroy the thing they need.
-- It is masked when the admin closes the request out.

CREATE OR REPLACE FUNCTION scrub_query_secrets(sql text) RETURNS text AS $$
  -- PASSWORD 'x', ENCRYPTED PASSWORD 'x', PASSWORD='x', WITH PASSWORD = N'x'
  -- (T-SQL). The keyword is what anchors it, so a literal that merely looks
  -- like a password is left alone.
  SELECT regexp_replace(
           COALESCE(sql, ''),
           '((?:ENCRYPTED\s+)?PASSWORD\s*=?\s*)(N?''[^'']*'')',
           '\1''***REDACTED***''',
           'gi');
$$ LANGUAGE sql IMMUTABLE;

COMMENT ON FUNCTION scrub_query_secrets(text) IS
  'Replace password literals in SQL text with ***REDACTED***. Used by the '
  'requests trigger so credentials do not persist in request history.';

CREATE OR REPLACE FUNCTION requests_scrub_secrets() RETURNS trigger AS $$
BEGIN
  IF NEW.status IN ('completed', 'failed', 'cancelled', 'rejected')
     AND NEW.query ~* '(?:encrypted\s+)?password\s*=?\s*N?''[^'']*'''
     AND NEW.query !~ '\*\*\*REDACTED\*\*\*'
  THEN
    NEW.query := scrub_query_secrets(NEW.query);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_requests_scrub_secrets ON requests;
CREATE TRIGGER trg_requests_scrub_secrets
  BEFORE UPDATE ON requests
  FOR EACH ROW
  EXECUTE FUNCTION requests_scrub_secrets();

-- Anything already terminal. Idempotent: the WHERE clause skips rows that
-- carry the marker, so re-running this migration is a no-op.
UPDATE requests
   SET query = scrub_query_secrets(query)
 WHERE status IN ('completed', 'failed', 'cancelled', 'rejected')
   AND query ~* '(?:encrypted\s+)?password\s*=?\s*N?''[^'']*'''
   AND query !~ '\*\*\*REDACTED\*\*\*';
