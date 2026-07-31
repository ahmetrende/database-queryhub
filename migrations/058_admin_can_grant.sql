-- Admin capability flag for the Slack-native grant/revoke tool (/sql grant).
-- Decouples "may hand out access from Slack" from full super-admin power:
-- an operator flips can_grant on for exactly the admins they trust with it.
-- Super-admins (unscoped: max_tier + scope_* all NULL) may grant regardless,
-- so this flag only matters for scoped admins.

ALTER TABLE admins
    ADD COLUMN IF NOT EXISTS can_grant boolean NOT NULL DEFAULT false;

-- Seed: every current super-admin can grant (they effectively already can,
-- out-of-band). Scoped admins stay false until explicitly flipped on.
UPDATE admins
   SET can_grant = true
 WHERE enabled
   AND max_tier IS NULL
   AND scope_team_ids IS NULL
   AND scope_target_ids IS NULL;
