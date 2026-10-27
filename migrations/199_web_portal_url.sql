-- The address of the portal, where a person goes when the own web UI is closed
-- to them (`web_own_ui_super_admin_only` is on).
--
-- A refused person lands on /portal-redirect, which says that QueryHub opens
-- through the portal and sends them to this address after 5 seconds. An empty
-- value shows the notice without a link. The address must be an absolute
-- http or https URL. A second run changes nothing, and an install that set the
-- key by hand keeps its value.

INSERT INTO bot_config (key, value, description) VALUES
    ('web_portal_url', '',
     'Absolute http(s) address of the portal. A person that the web UI refuses (web_own_ui_super_admin_only) lands on /portal-redirect, which sends them here after 5 seconds. Empty = the notice shows no link.')
ON CONFLICT (key) DO NOTHING;
