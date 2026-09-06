-- Identity assertions from the IDP panel.
--
-- The panel is a BFF: a browser never reaches this service, so there is no
-- cookie to present. Each proxied request instead carries a 60-second Ed25519
-- JWT naming the verified corporate address of the human behind it. That
-- address is resolved exactly as an OIDC login resolves one, through
-- requesters.by_email / admins.by_email, so the assertion adds a transport and
-- never a new authorization subject.
--
-- The jti ledger is what makes a captured token single-use. Rows live about
-- two minutes; the insert prunes, so no sweep job is needed at this volume.

CREATE TABLE IF NOT EXISTS idp_assertion_jti (
    jti        TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_idp_assertion_jti_expires
    ON idp_assertion_jti (expires_at);

-- Off by default. Until an operator turns it on, verify() refuses on its first
-- line and this service behaves exactly as it did before the seam existed.
INSERT INTO bot_config (key, value, description) VALUES
  ('idp_assertion_enabled', 'off',      'Accept IDP-signed identity assertions on /api routes.'),
  ('idp_issuer',            'idp',      'Expected iss claim on an IDP assertion.'),
  ('idp_audience',          'queryhub', 'Expected aud claim on an IDP assertion.'),
  ('idp_public_keys',       '{}',       'JSON object mapping kid -> PEM Ed25519 public key.'),
  ('idp_sync_principal',    '',         'Principal allowed to call the Layer-A reconcile endpoint.')
ON CONFLICT (key) DO NOTHING;
