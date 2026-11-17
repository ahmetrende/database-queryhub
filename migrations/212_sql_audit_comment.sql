-- 212: put the request id and the requester in front of each executed statement (2026-11-17).
--
-- Some managed database services keep only the statement text in their
-- audit log. They lose the application_name that names the request. With
-- 'on', the executor sends each statement as
--   /* QueryHub (<origin>) req_id=<id> by=<email> */ <statement>
-- requests.query does not change. The app reads the key for each request.
-- Safe to run again.

INSERT INTO bot_config (key, value, description) VALUES
    ('sql_audit_comment', 'off',
     'on | off. Put a comment with the origin, the request id and the requester email in front of each statement that QueryHub sends to a target.')
ON CONFLICT (key) DO NOTHING;
