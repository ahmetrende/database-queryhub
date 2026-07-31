-- Read-state for the web notification bell.
--
-- The developer notification feed itself is DERIVED on read (approval
-- decisions on the user's own requests, scheduled runs, endpoint-request
-- decisions, kill-switch flips) — nothing to store. What must persist is
-- which items the user has already seen, so the unread badge follows the
-- user across browsers/devices (the client also mirrors it in localStorage
-- for instant paint). Notification ids are deterministic strings derived
-- from the underlying row (e.g. q123-dec, er7, kill45).
CREATE TABLE IF NOT EXISTS web_notification_reads (
  slack_user_id TEXT NOT NULL,
  notif_id      TEXT NOT NULL,
  read_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (slack_user_id, notif_id)
);
