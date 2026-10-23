-- A switch for auto-approve windows that cover writes (RW).
--
-- A person can ask for an auto-approve window: for a set time, their queries
-- on one connection run with no review. A window for reads (RO) is always
-- possible. A window for writes is a policy decision, so this key controls it.
--
-- off (the default): QueryHub refuses a request for a RW window, on the web
-- and in Slack, with a message that says why. An admin cannot approve a RW
-- window that is still pending. An admin can reject it.
--
-- on: a person who holds RW on the database can ask for a RW window. A window
-- for schema changes (DDL) is never possible. Every window has an end time.
-- On a production connection, only a super-admin decides a RW window.
--
-- A second run changes nothing, and an install that set the key by hand keeps
-- its value.

INSERT INTO bot_config (key, value, description) VALUES
    ('auto_approve_rw_requests', 'off',
     'on = a person who holds RW on a database can ask for an auto-approve window that covers writes. An admin decides it, and on a production connection only a super-admin decides it. A window for schema changes (DDL) is never possible. off = only read-only (RO) windows can be asked for, and a pending RW window cannot be approved.')
ON CONFLICT (key) DO NOTHING;
