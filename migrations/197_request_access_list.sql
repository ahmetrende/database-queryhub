-- The request-access forms no longer list every connection.
--
-- The Slack modal, the QueryHub Web form and the IdP panel all offered a list
-- of every enabled connection, so that a requester could pick one they could
-- not use yet. The list showed the name and the host of each database to every
-- person on the whitelist. That is a map of the fleet, and nobody needed it to
-- ask for access.
--
-- With the list off, the requester types the server and the database. The
-- request reaches the admins with the typed text. An exact alias match is
-- resolved, so one click still approves it. Any other text stays free text, and
-- an admin matches it by hand.
--
-- `request_access_list_enabled` is the switch. The default is `off`. Set it to
-- `on` to bring the list back at once, with no restart. A second run changes
-- nothing, and an install that set the key by hand keeps its value.

INSERT INTO bot_config (key, value, description) VALUES
    ('request_access_list_enabled', 'off',
     'Show the list of connections in the request-access forms (Slack, QueryHub Web, IdP panel). "off" hides it: the requester types the server and the database, and an admin matches the text.')
ON CONFLICT (key) DO NOTHING;
