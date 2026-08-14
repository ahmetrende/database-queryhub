-- 095 — where a target actually runs: a tag bag per connection.
--
-- An RDS instance, a Huawei RDS instance and a SQL Server on a Huawei ECS box
-- look identical in the picker, and "where does this actually run" was answered
-- by nobody. Three keys are reserved and get real controls in the UI —
-- `provider` (aws | huawei | onprem), `service` (RDS · Aurora · EC2 · ECS ·
-- GaussDB · bare metal · VM) and `account` (a cloud account id, never a
-- credential) — and any other key is free text a DBA-admin invents.
--
-- On the CONNECTION, not the database. A database has no location, and a second
-- copy on every database is only a way for the two to disagree six months from
-- now; databases inherit the connection's bag and carry none of their own.
--
-- JSONB rather than a side table: the whole bag is replaced as a unit (a merge
-- patch cannot express "this key is gone", and a tag that survives its own
-- deletion keeps answering for a machine that has moved), it is read on every
-- /connections call, and it is never joined on. The CHECK keeps it an object —
-- a bare array or scalar would still be valid JSONB and would break every
-- reader.
--
-- Deliberately NOT a policy surface: grants and auto-approve stay per
-- connection/database. A rule like "auto-approve RO on AWS" would turn a text
-- field a DBA edits into a privilege boundary, and editing a label would become
-- privilege escalation.

ALTER TABLE target_servers
    ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'target_servers_tags_is_object'
    ) THEN
        ALTER TABLE target_servers
            ADD CONSTRAINT target_servers_tags_is_object
            CHECK (jsonb_typeof(tags) = 'object');
    END IF;
END $$;

COMMENT ON COLUMN target_servers.tags IS
    'Where this target runs: {provider, service, account, ...free keys}. '
    'Display-only — never a policy input. Replaced as a whole bag, never merged.';
