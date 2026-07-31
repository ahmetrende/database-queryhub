-- 063_web_session_avatar.sql
-- Carry the signed-in user's Slack avatar (image_192) on the web session
-- row so GET /api/me can return it as user.avatar WITHOUT a per-request
-- Slack API call, and so it survives refresh-token rotation (the session
-- row id is stable across rotations). Web-only: the Slack bot never reads
-- this column. Purely cosmetic — the UI falls back to initials when null.

ALTER TABLE web_sessions
    ADD COLUMN IF NOT EXISTS avatar_url TEXT;

COMMENT ON COLUMN web_sessions.avatar_url IS
    'Slack profile image (image_192) from the OIDC id_token at login; '
    'returned by /api/me as user.avatar. Cosmetic; UI falls back to initials.';
