-- Three settings for the IDP panel seam (docs/AUTH.md §1.2). All of them are
-- inert while idp_assertion_enabled is off.
--
--   idp_clock_skew_seconds     Seconds of clock disagreement tolerated between
--                              the panel and this host on an assertion's iat and
--                              exp. There was none: a panel clock one second
--                              ahead failed every assertion.
--   idp_outbox_enabled         Whether a pending submission writes a
--                              notification_outbox row for the panel. Off:
--                              nothing reads the table until the panel's poller
--                              is live, and before this switch existed it
--                              filled up for nothing.
--   idp_outbox_retention_days  Rows older than this are deleted by the daily
--                              cleanup (scripts/cleanup_old_results.py),
--                              processed or not. The table had no removal.
--
-- Migration 102's header is out of date on two points and cannot be edited,
-- because the migration ledger pins its checksum: the two endpoints it calls "a
-- later PR" shipped with it, and the writer uses admins.notify_list rather than
-- admins.list_active().

INSERT INTO bot_config (key, value, description) VALUES
  ('idp_clock_skew_seconds',    '10',  'Seconds of clock skew tolerated on an IDP assertion (0-60).'),
  ('idp_outbox_enabled',        'off', 'Write a notification_outbox row for the IDP panel on each pending submission.'),
  ('idp_outbox_retention_days', '7',   'Days a notification_outbox row is kept before the daily cleanup deletes it.')
ON CONFLICT (key) DO NOTHING;
