# Changelog

All notable changes to QueryHub are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). What the major number does and does not
cover is spelled out under "Versioning and support" in the README —
`bot_config` keys and the audit contract are inside the promise; the
frontend and the endpoints it calls are explicitly outside it.

## [Unreleased]

## [1.0.1] — 2026-07-31

### Fixed
- A query with an unbalanced quote came back as **"internal error"** instead of
  a syntax error. `ast_safety.check` catches `sqlglot.errors.ParseError` and
  fails closed with a message that already says "check for stray quotes" — but
  `TokenError` is a SIBLING of `ParseError`, not a subclass, and the tokenizer
  raises before a parse is attempted. So the one input the handler was written
  for was the one input that escaped it, all the way out of the API as a 500.
  Now the whole `SqlglotError` family is caught, with a backstop for anything
  new: a parser is a moving dependency on the submit path, and an exception
  class we have not seen must still refuse the query rather than reach a user.
- `POST /classify` reported `requiresJustification` as "DDL only", so the field
  the web editor is supposed to render was wrong for RW and ignored
  auto-approval entirely.

### Changed
- **A justification is no longer required when the request will be
  auto-approved.** The field's first reader is the approver, and an
  auto-approved request has no approver. The audit question is still answered:
  such a submission records the covering grant, and the grant carries its own
  reason. A SCHEDULED request stays exempt-free — its grant may lapse before
  the run time, which puts a human back in the loop.
- `/classify` now also publishes `requiresJustificationWhenScheduled`, because
  the client knows whether a schedule was picked and the endpoint does not.

### Known gap
- The web editor still has no justification input, so a user WITHOUT
  auto-approve cannot submit RW or DDL from the web. Slack's `/sql` modal has
  the field. The API contract this needs is now correct.

## [1.0.0] — 2026-07-31

The repository is public, and this is the first release built by the release
workflow rather than by hand. Nothing in the product changed for it: 1.0.0 marks
that the compatibility promise below is now in force, and that every install
path has been run end to end rather than assumed.

### Added
- A published container image: `ghcr.io/ahmetrende/database-queryhub:1.0.0`
  (and `:latest`). Pull it and set `QH_IMAGE` to skip the build. The compose
  file still builds from the checkout by default — an approval gateway is not
  something to upgrade by moving tag.

### Changed
- Comments and documentation no longer refer to a private tracker or a private
  working setup. 59 dangling issue IDs (`BUG-01`, `SEC-12`, …) are gone from the
  code, and `OPERATIONS.md` section 17 — which documented a script that is not
  part of an install — is now about the operational risk it was really pointing
  at: real identifiers leaking through screenshots, logs and result files.
- The two container CI jobs no longer skip themselves for dependency PRs. They
  are what catch a broken front door, which is what a dependency bump breaks.

### Fixed
- `MANIFEST.in` was missing `prune docs/screenshots`, so the screenshot index
  was pulled into the source distribution and `check_sdist_clean.py` failed on
  it — a blocking gate.

### Verified
- `docker compose -f docker-compose.install.yml up -d` builds, becomes healthy,
  applies the migrations and creates the bootstrap admin. All seven assertions
  in `scripts/ci_install_check.py` pass, including that the password printed in
  the container logs is good for nothing except changing itself.
- Full CI green on a public runner for the first time: unit tests on 3.11 and
  3.12, the real-database integration job, the optional-extras job, the vanilla
  (no-Slack) import job, the frontend build, and both container stacks.

## [0.1.0] — 2026-07-29

The initial cut, recorded for the history rather than as a shipped
artifact: no release, package or container image was ever produced for it.
`release.yml` did not exist yet, and a tag-triggered workflow is read from
the tagged ref, so nothing could have fired. 1.0.0 below is the first
release the workflow actually built.

### Install

- **Two-command install**: `docker-compose.install.yml` runs a released
  image against your own metadata Postgres, reading one `.env`. The first
  start generates the master key, applies the migrations and creates the
  admin account named by `QH_ADMIN_USER` — so nothing needs a shell in the
  container. Everything after that is in the UI: add a connection, test it,
  create a team, grant it.
- That first password is a **bootstrap** password: printed once if
  generated, and always carrying `must_change_pw`, so it is good for one
  login and cannot run a query. The account is created only if absent
  (`create_local_user.py --if-absent`), so a restart never resets a
  password the operator has changed.
- CI runs the install path end to end (`install-stack`, using the commands
  the README prints) and asserts it is clean — no demo accounts, no seeded
  connections — so the demo profile cannot leak into a deployment.

### Also in this release

- Access-request **approval auto-creates the per-user grant** it asks for
  (requester + target + database + requested tier; conservative on
  conflicts) — no more hand-run grant SQL for the common case.
- **Self-service password change** for local accounts, with a forced
  change flow for handed-off (`must_change_pw`) accounts.
- Hardening: access-request tier is taken only from the server-set field
  (never self-selectable via the reason text); the login brute-force
  throttle map is bounded; safe/typed `mypy` findings cleared.

### Core

- Admin-approved SQL gateway for **PostgreSQL** and **SQL Server**:
  submit → safety-check → approve → execute → deliver, with a full audit
  trail on every step.
- Three-tier execution (**RO / RW / DDL**): each query is classified
  (keyword + sqlglot AST second pass, engine-aware) and runs under the
  matching per-target credential — never above its tier.
- Team and per-user grants, scoped admins (tier / team / target), temp
  admin windows, time-bounded auto-approve exemptions, per-request
  fingerprint auto-approve, PII masking with column-level exemptions.
- Approving an access request auto-creates the per-user grant it asks
  for (requested tier, listed database) — no hand-run SQL for the
  common case.
- Pre-flight `EXPLAIN` with risk hints; per-user row limits; CSV/XLSX
  delivery with formula-injection neutralization; retention cleanup.

### Surfaces

- **Web UI** (FastAPI + React): SQL editor with schema-aware
  autocomplete, saved queries, history, server-paged results, Excel
  export, admin panel (approval queue, grants, config, audit, metrics),
  in-app notifications.
- **Slack** (optional `[slack]` extra): `/sql` modal, DM approvals,
  result delivery, schema browsing subcommands.

### Vanilla profile

- The base install runs with **zero external vendors** — no Slack, no
  cloud: web-only approvals, built-in **local accounts**
  (username/password; salted PBKDF2 hashes, never cleartext), guided
  installer (`scripts/install.sh`), brute-force login throttle.

### Security & operations

- Fernet-encrypted credentials at rest behind one master key; optional
  AWS Secrets Manager provider (`[aws]` extra).
- Migration ledger with checksums + advisory lock; graceful restarts
  that drain in-flight queries; MSSQL cross-database/linked-server
  blocking; `search_path` hardening; `SET LOCAL` value validation.
- Docs: install, prerequisites, configuration reference, architecture
  (core + adapters), auth/session design, operations, schema, known
  limitations. CI: lint + tests (3.11/3.12) + a no-Slack import gate.

[0.1.0]: https://github.com/ahmetrende/database-queryhub/releases/tag/v0.1.0
