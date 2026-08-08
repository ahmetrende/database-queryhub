# Changelog

All notable changes to QueryHub are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). What the major number does and does not
cover is spelled out under "Versioning and support" in the README —
`bot_config` keys and the audit contract are inside the promise; the
frontend and the endpoints it calls are explicitly outside it.

## [Unreleased]

## [1.0.8] — 2026-08-08

### Fixed

- **Run now honours a selection.** With text selected in the editor, Run — the
  toolbar button, F5 and Ctrl/Cmd+Enter alike — submits exactly that text instead
  of the whole tab. Previously only F8 did, so selecting one statement out of a
  multi-statement script and pressing Run submitted the entire script. A
  selection that contains only comments runs nothing and says so, rather than
  falling back to "run everything": highlighting a commented-out `UPDATE` must
  not execute the script around it. The rule lives in one function shared by all
  four entry points.
- **Autocomplete after a schema qualifier.** In a `FROM`/`JOIN`/`UPDATE` clause,
  `schema.tab|` offered columns — which cannot appear there — because the clause
  keyword was read from the word immediately before the caret, which is the
  qualifier itself. Accepting a qualified suggestion also wrote the schema twice
  (`dba.dba.whoisactive`), since the inserted text re-qualified while the replaced
  range covered only the bare name. A column pick after an alias still leaves the
  alias alone.
- **SQL Server icon.** The engine badge for SQL Server targets now uses
  Microsoft's current SQL Server 2025 mark.

### Added

- **Frontend tests.** `npm test` in `QueryHubWeb/app` runs `node --test` over the
  browser-side code, including the editor mounted in a DOM, and runs in CI after
  the bundle build. Frontend changes previously had no proof beyond compiling.

## [1.0.7] — 2026-08-06

### Added
- **A masking exemption can now be scoped to a schema and to super-admins.**
  The catalog matches column *names*, so an operator's own monitoring views come
  back mangled: a view exposing query text, host names or session owners trips
  the same rules a customer table does. Two new dimensions on
  `pii_masking_exemptions` carry it — `schema_name` and `super_admin_only` — and
  `target_server_id` becomes nullable so one row covers a whole fleet instead of
  drifting the moment a server is added.

  Both dimensions are needed. Scoping by the existing `table_name` would not
  work: it matches the **bare** name, so `dba.sessions` and `public.sessions` are
  one string to it and a toolkit exemption would unmask a business table that
  happens to share a name. And without the reader dimension the exemption is
  fleet-wide for everyone, which for `pg_stat_statements` means handing query
  literals to anyone with a grant.

  Two fail-closed rules: every table reference must be explicitly
  schema-qualified (an unqualified name resolves through `search_path`, so its
  schema is not knowable from the text), and every schema named must be exempt —
  one table from anywhere else and masking stays on for the whole result, the
  rule joins already follow. An unknown reader gets the strict answer, and so
  does a failed privilege lookup.

## [1.0.6] — 2026-08-06

### Added
- **Enabling a target without a credential is now refused.** Onboarding a
  discovered endpoint is two steps — make it visible, and give it a password —
  and only the first one is memorable, so the second gets skipped. Nothing used
  to complain: the target joined the picker, a grant could be issued on it, the
  tier badge rendered, and the schema snapshot failed quietly, so the tree showed
  a database with no tables. The failure surfaced on a user's screen instead of
  in the system. `set_enabled` is the single choke point every caller goes
  through, so the refusal lives there, names the target, and says what to do
  next; `force=True` still stages a target deliberately.
- `scripts/adopt_target_credential.py` — copies a credential from a sibling
  target for fleets where one database role serves every instance. It moves the
  **ciphertext**, so the secret is never read, printed or passed as an argument,
  and it refuses when the two targets use different role names, because that
  produces a login failure that looks like a password problem. Dry run by
  default; `--check` reports enabled targets that still hold the placeholder.

## [1.0.5] — 2026-08-06

### Fixed
- **The maintenance database could reach a user's sidebar.** `postgres` is hidden
  from every listing on purpose, and it is also `default_database` on most of this
  fleet. `GET /connections` filtered the hidden names *before* falling back to the
  default database, so a target with an empty catalog — which is every target
  between being enabled and its first schema snapshot — fell back to exactly the
  database meant to be invisible, past a filter that had already run. A user saw it
  in the tree. The filter now runs after the fallback and covers both paths in one
  pass, and a target left with nothing after filtering is dropped rather than
  falling back a second time.

## [1.0.4] — 2026-08-01

### Changed
- The GitHub Actions the workflows call are current again: `checkout` 4→7,
  `setup-node` 4→7, `download-artifact` 4→8, `login-action` 3→4.5.2 and
  `build-push-action` 6→7. No shipped code changes; this release exists so the
  build that produces the artifacts is not running on abandoned action majors.

## [1.0.3] — 2026-07-31

### Added
- **A reason field in the web editor.** Ported from the design source: a strip
  between the action bar and the editor, not a dialog in front of Run, because the
  reason is part of the request. Run stays enabled with an empty required field —
  it focuses and rings it rather than doing nothing, since a disabled button plus
  a keyboard shortcut is a no-op nobody can diagnose. The strip appears only on a
  settled classification and never disappears, so typed text is not destroyed by a
  keystroke that drops the statement back to read-only. Recent reasons are offered
  as chips and never prefilled.
- **`geography`, `geometry` and `hierarchyid` are readable.** They arrive as SQL
  Server's own serialisation, which we could only render as hex. The executor now
  asks the server for the result shape without running the query, and where one of
  those types appears, re-projects that column through `.ToString()` —
  `POINT (28.9784 41.0082)`, `/1/2/3/`. Read-only queries only, and the server
  decides: a top-level `ORDER BY` makes a query unwrappable, so the wrapped form is
  attempted and the original runs if it is refused.
- The sidebar makes room for long target names: the fleet-wide name head is folded
  and dimmed, an ambiguous database row names its server on a second line, and
  double-clicking the resizer fits the pane to its widest row.

### Fixed
- `POST /classify` publishes `requiresJustificationWhenReviewed` — named for the
  question it answers, "will a human read this?" The old
  `requiresJustificationWhenScheduled` is kept as an alias for one release. It gave
  the right answer for the wrong reason: a batch also always meets an approver, and
  a field whose name and meaning agree only by accident is one rename from a silent
  bug.
- `GET /admin/queue` emits `justification` as well as `reason`. The requester
  submits `justification`; the approver was reading the same value under a name that
  existed only because the queue mapper was written separately.
- The Slack modal no longer labels the justification field "(optional)". It is
  required for write and DDL unless an auto-approve grant covers it, and the hint
  now says so instead of promising the opposite.

## [1.0.2] — 2026-07-31

### Fixed
- The audit log described a web sign-in as "via slack", which read as a
  contradiction next to its own title, "Signed in to web". The data was right —
  someone signed into the web app using Slack SSO — but "via" was already taken
  in the same list, where a submitted request renders "via web" or "via Slack"
  to mean the surface it arrived from. One word, two meanings, one list. A
  sign-in now names its identity provider instead: "Slack SSO", "local account",
  or "<name> sign-in" for a provider this build does not know, because a login
  nobody can attribute is exactly what an audit reader needs to see.

### Changed
- The example image tag in `README.md`, `.env.example` and
  `docker-compose.install.yml` is now checked against `pyproject.toml` by a
  test. Naming a version rather than `latest` is deliberate — a gateway should
  not change underneath an operator because a tag moved — and the cost of that
  advice is a number that has to be maintained. It was not: the README went on
  advertising 1.0.0 after 1.0.1 shipped, which sends a reader to a real image
  that is not the one being shipped.

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
