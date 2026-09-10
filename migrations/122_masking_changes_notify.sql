-- Masking exemptions join the auth-event outbox.
--
-- Twenty-one triggers cover the tables that decide who may READ what, and the
-- person affected is DM'd whenever one of them changes -- including when the
-- change is made straight from psql. `pii_masking_exemptions` decides what is
-- SHOWN once someone reads, and it had no trigger: switching masking off for a
-- production column notified nobody, which is exactly backwards for the one
-- table in the set whose rows remove a protection rather than grant a power.
--
-- The recipient is different in kind, and that is why this was not just an
-- oversight to fix by adding the table to migration 060's list. Every other
-- auth table names its subject in the row (`slack_user_id`), so the DM has an
-- obvious addressee. An exemption has no subject: it changes what EVERY reader
-- of that column sees. So the trigger writes the event with a NULL user and the
-- poller fans it out to the admins -- the people who can undo it.
--
-- The capture function is reused unchanged: it already tolerates a row with
-- neither `slack_user_id` nor `team_id`, and it already honours the
-- `app.auth_dm_suppress` GUC, so a bulk operator edit can stay silent the same
-- way the pod onboarding did.

DROP TRIGGER IF EXISTS trg_auth_event ON pii_masking_exemptions;
CREATE TRIGGER trg_auth_event
    AFTER INSERT OR UPDATE OR DELETE ON pii_masking_exemptions
    FOR EACH ROW EXECUTE FUNCTION auth_event_capture();
