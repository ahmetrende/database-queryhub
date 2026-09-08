-- The replacement access model: nine tables that answer what seventeen answer
-- today, and answer it with the database enforcing the answer.
--
-- Purely additive. Nothing here reads or alters an existing table except three
-- nullable columns on `requests`, which is a catalogue-only change. No code
-- reads these tables yet: the resolver still runs on `teams` /
-- `team_target_grants` / `user_target_grants`, and switches over only once
-- `scripts/access_snapshot.py compare` is green against the baseline captured
-- before any of this existed.
--
-- WHY, in one paragraph. Today a principal is a bare text string in 53 columns
-- across 37 tables with not one foreign key pointing at `requesters` or
-- `admins`: deleting a person leaves orphan rows the database never notices.
-- Access arrives by six separate paths (admin row, bypass flag, temp-admin
-- table, user grant, team grant, auto-approve table) with two vocabularies for
-- one concept — grants say `mode`, escalation says `max_tier`, and `ddl` never
-- appears in the second. Here there are two paths, one vocabulary, and every
-- principal reference is a foreign key.
--
-- THE RULES THIS SCHEMA ENCODES
--
--   Access comes from `access_grant` and, for `role = 'admin'` only, from
--   `role_assignment`. The other roles grant no access at all.
--
--   A grant covers a query when (all_targets OR target_id matches) AND
--   (all_databases OR database_name matches). Wildcards are named booleans,
--   never a bare NULL: `NULL` alone made "everything" and "nothing" look
--   identical in a column, and a scope typed `{}` instead of NULL once handed
--   out fleet-wide approval rights.
--
--   Principal rows beat team rows. If any covering principal row says
--   `merge_with_team = false` (the default, and today's behaviour) the team's
--   rows are ignored entirely — that is what lets a row NARROW what a team
--   allows. A global switch was rejected: flipping one would change everyone's
--   access with no grant row changing and no notification firing.
--
--   Expiry and revocation are different endings and stay separate columns.
--   `valid_until` is the planned end; an expired principal row must NOT fall
--   through to the team, because such a row is usually written to narrow, and
--   falling through would mean an expiry that WIDENS access. `revoked_at` is
--   the unplanned end, and does fall through — that behaviour predates expiry
--   and is preserved deliberately.
--
--   One row per database. `database_name` NULL (with `all_databases`) means
--   every database. Arrays are gone: 100 of the 120 grants that exist today
--   already say "every database", the empty-array case has never once been
--   used, and per-row databases remove by construction the cross-product bug
--   `teams.effective_mode_for_database` exists to work around.
--
-- Rows here name real people and real endpoints, so — same rule as
-- `mssql_host_map` — the schema lives in the repo and the data is loaded at
-- runtime.

-- ---------------------------------------------------------------------------
-- tier: the vocabulary, as data
-- ---------------------------------------------------------------------------
--
-- A CHECK constraint would hard-code three tiers into every table that
-- mentions one, so a deployment that needs a fourth would need a migration to
-- express it. `rank` is what "the most permissive wins" compares.
CREATE TABLE IF NOT EXISTS tier (
  name        TEXT PRIMARY KEY,
  rank        INTEGER     NOT NULL UNIQUE,
  description TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO tier (name, rank, description) VALUES
  ('ro',  10, 'read only'),
  ('rw',  20, 'read and write'),
  ('ddl', 30, 'schema changes')
ON CONFLICT (name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- principal: who exists
-- ---------------------------------------------------------------------------
--
-- `enabled` IS the whitelist, and defaults to FALSE: a directory sync may
-- create a principal, but only a human decision turns one on. `kind`
-- separates a person from a machine account, which `provider` cannot answer —
-- provider says how something authenticates, not what it is. A service account
-- must be excluded from person pickers, notification fan-out and product
-- metrics, and "which non-human identities can reach production" has to be one
-- WHERE clause at audit time.
CREATE TABLE IF NOT EXISTS principal (
  id           BIGSERIAL PRIMARY KEY,
  kind         TEXT NOT NULL DEFAULT 'person'
                 CHECK (kind IN ('person', 'service')),
  display_name TEXT,
  email        TEXT,
  tz           TEXT,
  enabled      BOOLEAN     NOT NULL DEFAULT FALSE,
  created_by   BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  is_deleted   BOOLEAN     NOT NULL DEFAULT FALSE,
  deleted_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Partial, so a soft-deleted row does not block re-adding the same address.
-- Every uniqueness rule in this file is partial for that reason.
CREATE UNIQUE INDEX IF NOT EXISTS principal_email_uq
  ON principal (lower(email)) WHERE email IS NOT NULL AND NOT is_deleted;

-- ---------------------------------------------------------------------------
-- principal_identity: how they get in
-- ---------------------------------------------------------------------------
--
-- Separate from `principal` because one human has several logins: Slack today,
-- company SSO now, a portal assertion next. Carrying provider + external_id on
-- the principal itself would make the same person two rows with the grants on
-- only one of them. It also retires the e-mail match the SSO path uses today:
-- an address can change hands, an OIDC `sub` cannot.
CREATE TABLE IF NOT EXISTS principal_identity (
  id           BIGSERIAL PRIMARY KEY,
  principal_id BIGINT NOT NULL REFERENCES principal (id) ON DELETE RESTRICT,
  provider     TEXT NOT NULL,          -- slack | local | oidc:<id> | idp
  external_id  TEXT NOT NULL,          -- slack user id, username, oidc sub
  created_by   BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  is_deleted   BOOLEAN     NOT NULL DEFAULT FALSE,
  deleted_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS principal_identity_uq
  ON principal_identity (provider, external_id) WHERE NOT is_deleted;
CREATE INDEX IF NOT EXISTS idx_principal_identity_principal
  ON principal_identity (principal_id);

-- ---------------------------------------------------------------------------
-- principal_credential: what they prove it with
-- ---------------------------------------------------------------------------
--
-- One table for a local password hash, an API key and a portal's public key,
-- because they are the same question asked three ways. The portal key lives in
-- `bot_config.idp_public_keys` today, which is a settings table read through
-- the admin config screen — not where a trust anchor belongs.
CREATE TABLE IF NOT EXISTS principal_credential (
  id           BIGSERIAL PRIMARY KEY,
  principal_id BIGINT NOT NULL REFERENCES principal (id) ON DELETE RESTRICT,
  kind         TEXT NOT NULL CHECK (kind IN ('password', 'api_key', 'public_key')),
  material     TEXT NOT NULL,          -- hash; for public_key, the key itself
  label        TEXT,                   -- kid, or a name for a key its owner picked
  must_change  BOOLEAN     NOT NULL DEFAULT FALSE,
  last_used_at TIMESTAMPTZ,
  valid_until  TIMESTAMPTZ,
  created_by   BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  is_deleted   BOOLEAN     NOT NULL DEFAULT FALSE,
  deleted_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_principal_credential_principal
  ON principal_credential (principal_id) WHERE NOT is_deleted;

-- ---------------------------------------------------------------------------
-- team: a group, whatever the source calls it
-- ---------------------------------------------------------------------------
--
-- `name` is the stable code a grant and a log line reference; `display_name`
-- is what a person reads and may change under it. `source` says who owns the
-- membership: anything other than 'manual' is written by a sync and is
-- read-only in the UI, or a hand-added member disappears at the next run.
-- `attributes` absorbs whatever a particular source carries — board names,
-- open-work counts, org labels — so no external system's vocabulary has to
-- enter this schema to be recorded.
CREATE TABLE IF NOT EXISTS team (
  id           BIGSERIAL PRIMARY KEY,
  name         TEXT NOT NULL,
  display_name TEXT NOT NULL,
  source       TEXT NOT NULL DEFAULT 'manual',
  external_id  TEXT,
  attributes   JSONB       NOT NULL DEFAULT '{}',
  enabled      BOOLEAN     NOT NULL DEFAULT TRUE,
  created_by   BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  is_deleted   BOOLEAN     NOT NULL DEFAULT FALSE,
  deleted_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(attributes) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS team_name_uq
  ON team (name) WHERE NOT is_deleted;
CREATE UNIQUE INDEX IF NOT EXISTS team_external_uq
  ON team (source, external_id) WHERE external_id IS NOT NULL AND NOT is_deleted;

-- ---------------------------------------------------------------------------
-- team_member: who is in it
-- ---------------------------------------------------------------------------
--
-- `is_lead` lives on the membership rather than as a column on `team`, so a
-- lead is necessarily a member and the two facts cannot disagree. Being lead
-- grants nothing on its own: approval rights are a `role_assignment` row, so
-- that a directory sync can never hand someone the power to approve.
CREATE TABLE IF NOT EXISTS team_member (
  id           BIGSERIAL PRIMARY KEY,
  team_id      BIGINT NOT NULL REFERENCES team (id)      ON DELETE RESTRICT,
  principal_id BIGINT NOT NULL REFERENCES principal (id) ON DELETE RESTRICT,
  is_lead      BOOLEAN     NOT NULL DEFAULT FALSE,
  created_by   BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  is_deleted   BOOLEAN     NOT NULL DEFAULT FALSE,
  deleted_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS team_member_uq
  ON team_member (team_id, principal_id) WHERE NOT is_deleted;
CREATE INDEX IF NOT EXISTS idx_team_member_principal
  ON team_member (principal_id) WHERE NOT is_deleted;

-- ---------------------------------------------------------------------------
-- access_grant: the one matrix
-- ---------------------------------------------------------------------------
--
-- Replaces `team_target_grants`, `user_target_grants` and
-- `auto_approve_grants`. The subject is two nullable foreign keys with a check
-- that exactly one is set, NOT one polymorphic `subject_id`: a single column
-- cannot reference two tables, so it could carry no foreign key, which is the
-- defect this whole migration exists to remove.
--
-- `auto_approve` is a column, not a table. A grant and a permission to skip
-- the wait are the same row seen twice, and expressing "read is automatic,
-- write needs an approval" as two rows is clearer than two tables that have to
-- be kept consistent. The tier matters: a query is only waved through by a row
-- whose tier is at least the tier the query needs.
--
-- `db_role` is carried over from `team_target_grants.target_role` — the role
-- the session assumes after connecting. Live in the executor, unused in any
-- row today, and worth keeping: it ties a grant to a database role directly.
CREATE TABLE IF NOT EXISTS access_grant (
  id              BIGSERIAL PRIMARY KEY,
  principal_id    BIGINT REFERENCES principal (id)      ON DELETE RESTRICT,
  team_id         BIGINT REFERENCES team (id)           ON DELETE RESTRICT,
  target_id       BIGINT REFERENCES target_servers (id) ON DELETE RESTRICT,
  all_targets     BOOLEAN     NOT NULL DEFAULT FALSE,
  database_name   TEXT,
  all_databases   BOOLEAN     NOT NULL DEFAULT FALSE,
  tier            TEXT        NOT NULL REFERENCES tier (name),
  auto_approve    BOOLEAN     NOT NULL DEFAULT FALSE,
  merge_with_team BOOLEAN     NOT NULL DEFAULT FALSE,
  db_role         TEXT,
  valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_until     TIMESTAMPTZ,
  revoked_at      TIMESTAMPTZ,
  revoked_by      BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  reason          TEXT,
  created_by      BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  is_deleted      BOOLEAN     NOT NULL DEFAULT FALSE,
  deleted_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- exactly one subject
  CONSTRAINT access_grant_one_subject
    CHECK ((principal_id IS NULL) <> (team_id IS NULL)),
  -- the wildcard and the reference are two spellings of one fact, so they
  -- cannot disagree: an empty target with the flag unset is not writable
  CONSTRAINT access_grant_target_wildcard
    CHECK (all_targets = (target_id IS NULL)),
  CONSTRAINT access_grant_database_wildcard
    CHECK (all_databases = (database_name IS NULL)),
  -- naming a database while covering every target would describe a database
  -- name on servers nobody checked
  CONSTRAINT access_grant_database_needs_target
    CHECK (all_databases OR NOT all_targets),
  -- merging with the team is only meaningful for a principal's own row
  CONSTRAINT access_grant_merge_is_principal_only
    CHECK (NOT merge_with_team OR principal_id IS NOT NULL),
  CONSTRAINT access_grant_window_ordered
    CHECK (valid_until IS NULL OR valid_until > valid_from)
);

-- One live row per (subject, scope, tier, auto_approve). NULLS NOT DISTINCT is
-- what makes this work at all: the wildcard columns are NULL, and under the
-- default NULLS DISTINCT two identical wildcard grants would both be allowed.
CREATE UNIQUE INDEX IF NOT EXISTS access_grant_live_uq
  ON access_grant (principal_id, team_id, target_id, database_name, tier,
                   auto_approve) NULLS NOT DISTINCT
  WHERE NOT is_deleted AND revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_access_grant_principal
  ON access_grant (principal_id) WHERE NOT is_deleted AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_access_grant_team
  ON access_grant (team_id) WHERE NOT is_deleted AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_access_grant_target
  ON access_grant (target_id) WHERE NOT is_deleted AND revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- role_assignment: who approves, who grants, who administers
-- ---------------------------------------------------------------------------
--
-- Replaces the `admins` scope arrays, `temp_admin_grants` and `import_grants`.
-- Temporary admin is not a separate table, only a row with `valid_until`.
--
-- `admin` is the one role that carries access, which is why it must be
-- unscoped: a super-admin is one row rather than a role row plus a wildcard
-- grant row that someone can forget to write. The other three carry none.
--
-- A team lead who may approve only their own team's requests, to `rw`, is:
--   role='approver', scope_team_id=<their team>, max_tier='rw',
--   all_teams=false, all_targets=true, any_tier=false.
CREATE TABLE IF NOT EXISTS role_assignment (
  id              BIGSERIAL PRIMARY KEY,
  principal_id    BIGINT NOT NULL REFERENCES principal (id) ON DELETE RESTRICT,
  role            TEXT NOT NULL
                    CHECK (role IN ('approver', 'granter', 'importer', 'admin')),
  scope_team_id   BIGINT REFERENCES team (id)           ON DELETE RESTRICT,
  all_teams       BOOLEAN     NOT NULL DEFAULT FALSE,
  scope_target_id BIGINT REFERENCES target_servers (id) ON DELETE RESTRICT,
  all_targets     BOOLEAN     NOT NULL DEFAULT FALSE,
  max_tier        TEXT        REFERENCES tier (name),
  any_tier        BOOLEAN     NOT NULL DEFAULT FALSE,
  valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_until     TIMESTAMPTZ,
  revoked_at      TIMESTAMPTZ,
  revoked_by      BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  reason          TEXT,
  created_by      BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  is_deleted      BOOLEAN     NOT NULL DEFAULT FALSE,
  deleted_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT role_assignment_team_wildcard
    CHECK (all_teams = (scope_team_id IS NULL)),
  CONSTRAINT role_assignment_target_wildcard
    CHECK (all_targets = (scope_target_id IS NULL)),
  CONSTRAINT role_assignment_tier_wildcard
    CHECK (any_tier = (max_tier IS NULL)),
  -- admin carries access, so it is never partial
  CONSTRAINT role_assignment_admin_is_unscoped
    CHECK (role <> 'admin' OR (all_teams AND all_targets AND any_tier)),
  CONSTRAINT role_assignment_window_ordered
    CHECK (valid_until IS NULL OR valid_until > valid_from)
);

CREATE INDEX IF NOT EXISTS idx_role_assignment_principal
  ON role_assignment (principal_id) WHERE NOT is_deleted AND revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- principal_setting: the per-person dials
-- ---------------------------------------------------------------------------
--
-- Replaces `user_row_limit_overrides` and `report_excluded_users`: two tables
-- holding three rows between them. Key/value is the right shape for rare
-- per-person exceptions, and a new one costs no migration.
CREATE TABLE IF NOT EXISTS principal_setting (
  id            BIGSERIAL PRIMARY KEY,
  principal_id  BIGINT NOT NULL REFERENCES principal (id) ON DELETE RESTRICT,
  setting_key   TEXT NOT NULL,        -- max_rows | exclude_from_metrics
  setting_value TEXT NOT NULL,
  valid_until   TIMESTAMPTZ,
  reason        TEXT,
  created_by    BIGINT      REFERENCES principal (id) ON DELETE SET NULL,
  is_deleted    BOOLEAN     NOT NULL DEFAULT FALSE,
  deleted_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS principal_setting_uq
  ON principal_setting (principal_id, setting_key) WHERE NOT is_deleted;

-- ---------------------------------------------------------------------------
-- updated_at, once
-- ---------------------------------------------------------------------------
--
-- A trigger rather than nine sets of application code that each have to
-- remember. A hand-maintained `updated_at` is wrong exactly when it matters:
-- the row somebody changed without going through the usual path.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['tier', 'principal', 'principal_identity',
                           'principal_credential', 'team', 'team_member',
                           'access_grant', 'role_assignment',
                           'principal_setting']
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgname = 'trg_' || t || '_updated_at'
                      AND tgrelid = t::regclass) THEN
      EXECUTE format(
        'CREATE TRIGGER %I BEFORE UPDATE ON %I '
        'FOR EACH ROW EXECUTE FUNCTION set_updated_at()',
        'trg_' || t || '_updated_at', t);
    END IF;
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- requests: which rule allowed this
-- ---------------------------------------------------------------------------
--
-- Nullable, no default: a catalogue-only change on the busiest table.
--
-- The gap this closes is the one an audit asks about first. There are 5564
-- requests on record and not one of them says which grant permitted it — only
-- the tier it needed. Recording the row makes the question answerable, and it
-- is also why a grant's scope and tier are never edited in place: change means
-- revoke and re-create, so an old request keeps pointing at the rule that was
-- actually in force when it ran.
ALTER TABLE requests
  ADD COLUMN IF NOT EXISTS access_grant_id    BIGINT REFERENCES access_grant (id),
  ADD COLUMN IF NOT EXISTS role_assignment_id BIGINT REFERENCES role_assignment (id),
  ADD COLUMN IF NOT EXISTS approved_by_principal_id BIGINT REFERENCES principal (id);
