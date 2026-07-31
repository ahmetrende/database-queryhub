-- Capture each Slack user's IANA timezone alongside email so the schedule
-- picker can interpret the user's wall-clock pick in their local time. Lazily
-- backfilled by the bot via `users.info` on first interaction.

ALTER TABLE requesters ADD COLUMN IF NOT EXISTS tz TEXT;
ALTER TABLE admins     ADD COLUMN IF NOT EXISTS tz TEXT;

COMMENT ON COLUMN requesters.tz IS
$$IANA timezone (e.g. "Europe/Istanbul") from Slack users.info. Used to interpret schedule-picker values as the user's local wall-clock time. NULL = not yet backfilled; bot falls back to UTC.$$;

COMMENT ON COLUMN admins.tz IS
$$IANA timezone (e.g. "Europe/Istanbul") from Slack users.info. Same role as requesters.tz.$$;
