-- Fix 129: its trigger named a status the request_status enum does not have.
--
-- `NEW.status IN (..., 'expired', ...)` casts every literal to the enum, and
-- 'expired' is not a member, so the IF raised on EVERY insert and update of
-- `requests` -- no request could be submitted, approved or finished until
-- this ran. Compared as text, a name the enum lacks simply never matches, so
-- this list can name statuses defensively without being able to take the
-- table down again.

CREATE OR REPLACE FUNCTION requests_scrub_secrets() RETURNS trigger AS $$
BEGIN
  IF NEW.query ~* '(?:encrypted\s+)?password\s*=?\s*[EN]?''' THEN
    NEW.query := scrub_query_secrets(NEW.query);
  END IF;
  IF NEW.status::text IN ('completed', 'failed', 'cancelled', 'rejected',
                          'expired', 'changes_requested', 'awaiting_dba_manual') THEN
    NEW.query_secret := NULL;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
