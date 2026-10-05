# QueryHub Roadmap

QueryHub is an admin-approved, fully-audited SQL gateway that you can self-host:

1. Developers submit SQL.
2. An admin approves it.
3. The gateway executes it with tier-matched (RO / RW / DDL) credentials.
4. The gateway masks PII in the result.

The gateway audits every step.

This roadmap makes one promise concrete: **QueryHub should run for anyone, on
anything, with no mandatory vendor**. It is not tied to a specific cloud, to
Slack, or to a single database engine. You bring a database and a way to sign
in. Everything else is optional and swappable.

A SQL gateway is a *security boundary*, so this roadmap holds it to a higher bar
than an ordinary app. The trust chain from "what was approved" to "what actually
ran" must be durable, re-checked, and impossible to widen from the query itself.

---

## Design principle — ports & adapters

The core pipeline is small, and it does not change:

```
submit → classify (RO/RW/DDL) → approve → execute → deliver → audit
```

The core depends only on **interfaces (ports)**. Every external concern is an
**adapter** that configuration selects. Every port ships a **zero-dependency
default**, so a fresh install works with nothing but a database.

| Port | What it does | Default (zero-dep) | Optional adapters |
|------|--------------|--------------------|-------------------|
| **Identity** | who is signing in | Local users / generic OIDC | Slack OIDC, SAML, reverse-proxy header, email magic-link |
| **Approval + notify** | fan-out to approvers, collect the decision, tell the requester | Web admin panel + in-app notification bell | Slack, email (SMTP), webhook, Microsoft Teams |
| **Secrets** | target DB credentials at rest / just-in-time | Local encrypted vault (Fernet) | AWS Secrets Manager, HashiCorp Vault, dynamic (IAM), env vars |
| **Engine** | the data source that QueryHub queries | PostgreSQL | MySQL, SQL Server, ClickHouse, (Snowflake / BigQuery / Redshift later) |
| **Artifacts** | where result files live | Local filesystem | S3, GCS, Azure Blob |
| **Metadata store** | QueryHub's own control-plane data | PostgreSQL | SQLite (small / single-node) |

### The "vanilla" profile — zero silly dependencies

This is the default install that a newcomer gets:

> PostgreSQL metadata + a PostgreSQL target + local encrypted vault +
> **web-only** approval (no Slack) + local-filesystem results + email/none
> notifications. A single `docker compose up` starts all of it.

No cloud account. No Slack workspace. No message broker. Slack, AWS, and every
other integration are strictly opt-in.

---

## Security & correctness invariants (the trust chain)

These are the properties to measure every release against. They are goals that
the codebase moves toward, not claims about today. The phases below show how the
project meets them. No convenience flag can ever disable any of them.

- **Durable, single, atomic execution.** An approved job survives a process
  crash and runs *exactly once*. A queue/outbox backs dispatch. A worker claims
  a job with a compare-and-set (only an un-run job can start), never with a
  best-effort in-memory hand-off.
- **Re-authorized at execution.** QueryHub re-checks grants, membership, allowed
  databases and policy at run time, not just at submit. A revoked grant stops a
  queued or scheduled job.
- **Resource + scope limits the query cannot widen.** The executor and the
  target's own privileges enforce the statement timeout, memory, row/byte caps
  and database/schema scope. User SQL cannot raise a limit or reach another
  database, catalog, or server.
- **Safe-by-default approval.** Write and schema changes need a second party by
  default. Any time-bounded auto-approval is parameterized (bounded inputs), not
  literal-blind pattern matching.
- **Tamper-evident, immutable audit.** The runtime role cannot rewrite the audit
  trail. The trail is append-only, with a hash-chain / external WORM-or-SIEM
  sink option.
- **Least privilege everywhere.** The metadata runtime role is not the schema
  owner. Target credentials cover only what a tier needs, not blanket
  fleet-wide roles.

---

## Current state (honest baseline)

Already decoupled or close:

- **Approval engine is channel-free.** `core_submit` / `core_decide` are
  transport-agnostic. The web admin panel already approves/rejects without
  Slack. Slack is a lazy import, and only notification delivery uses it.
- **Engine dispatch exists**, and it fails closed on an engine that it cannot
  run. Postgres is the default, not a hard-wired assumption. A SQL Server path
  exists behind an extra.
- **No cloud in the hot path.** All cloud/provisioning code lives in operator
  scripts. No cloud SDK is a core dependency.
- **SQL safety is two-pass** (keyword allow-list + a `sqlglot` AST pass) and
  dialect-capable.

Honest caveats to close:

- Result **delivery** and the result **path** are not yet ports.
- The metadata store is PostgreSQL-specific.
- Type checking is not yet clean: CI blocks only *new* `mypy` errors, measured
  against a committed baseline.
- **One process, no HA.** The scheduler and boot recovery run in a single
  process. Dispatch is `SKIP LOCKED`-safe, but boot recovery is not. So a second
  web replica in the vanilla profile would double-run it. The login throttle is
  per-process for the same reason. This is the largest architectural limit. See
  [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

These items closed after this section was written. The list keeps them visible,
so it stays falsifiable rather than flattering:

- ~~"300+ tests, mostly mocked, no real DB in CI"~~ → **989 tests**. A CI job
  also runs real-DB integration tests against a Postgres service container, and
  fails if they skip.
- ~~"Slack is still a core packaging dependency"~~ → it is the `[slack]` extra.
  A `vanilla-import` CI job proves that the base install imports the web, core
  and executor without it.
- ~~"no container yet"~~ → `Dockerfile` + `docker-compose.yml` start the app,
  its metadata database and a seeded demo target. A CI job drives a full
  submit → approve → execute round trip against it.
- ~~"Dependabot/Renovate" (was P2)~~ → `.github/dependabot.yml` is in place.
- ~~"PII region packs" (was P3)~~ → the pack mechanism exists (`pii_region`,
  generic + one region). What remains is *more* packs, not the mechanism.
- ~~"No operational telemetry: no metrics endpoint, no structured logs"~~ →
  `GET /metrics` in Prometheus text format (off by default, bearer token or
  admin session) and `LOG_FORMAT=json`. QueryHub computes the values from SQL at
  scrape time, so they survive restarts and do not differ between the two
  processes. No quantiles: see [docs/OPERATIONS.md](docs/OPERATIONS.md#24-monitoring-metrics-and-structured-logs).
- ~~"One encryption key, no rotation tooling"~~ → the master key file is a ring
  (line 1 is the primary, older lines still decrypt). So you can introduce a new
  key without downtime. `scripts/rotate_master_key.py` does the re-encryption
  pass: dry-run by default, one transaction, round-trip verified, resumable.
  [docs/KEY_ROTATION.md](docs/KEY_ROTATION.md) is the procedure.

---

## Phase milestones

| Phase | Milestone | Theme |
|-------|-----------|-------|
| **P0** | publish-safe | security + correctness + legal blockers to close before the repo is public |
| **P1** | public alpha | anyone can run it (packaging) + no mandatory vendor (decoupling) + web/auth/transport hardening |
| **P2** | public beta / production-grade | multi-engine, reliability at scale, pluggable secrets, immutable audit, observability |
| **P3** | v1.0 | differentiation: signed execution contract, policy-as-code, and the productization long tail |

Leverage and safety set the phase order. The phases are not a rigid sequence.

---

## P0 — Publish-safe gate

Close these items before the repository goes public, or before any "try me"
artifact ships. (Detailed, reproduction-level security notes live in private
tracking, not here.)

**Execution trust chain**
- [ ] Enforce server-side resource limits that a query cannot override. Check
      timeout, memory and planner knobs by value and range, not just by name.
- [ ] Durable execution: queue/outbox + atomic compare-and-set claim, so that a
      crash cannot lose an approved job, and the job cannot run twice.
      *(Check against the existing execution-lease table. Part of this may
      already hold.)*
- [ ] Re-authorize at execution time (re-check grant / enabled /
      allowed-database / policy after a worker claims the job).

**Scope safety**
- [ ] Harden PostgreSQL object resolution with a `pg_catalog`-first search path.
      Flag writable-schema targets at onboarding.
- [ ] Confine SQL Server to the selected database. Reject cross-catalog and
      linked-server identifiers unless a per-target allow-list explicitly allows
      them.
- [ ] Persist the engine + required tier on the request. Re-check them at
      approval, so that tier classification is always right for the engine.

**Result safety**
- [x] Neutralize spreadsheet formula injection in CSV/XLSX export (after
      masking), the header row included (Unreleased).
- [ ] Fix large-result XLSX export. *(Reproduce first to check the exact
      boundary.)*

**Approval defaults**
- [ ] Require a second approver for RW/DDL by default. Disable self-approval by
      default, with an audited break-glass path.
- [ ] Disable auto-approval by default. Replace literal-blind fingerprints with
      admin-approved parameterized templates (bounded inputs).

**Release hygiene**
- [ ] Migration runner: applied-version ledger + checksum + advisory lock +
      dirty-state handling. *(Check current behavior. The runner already appears
      to track applied migrations. Check what is missing.)*
- [ ] Single, consistent license across LICENSE / NOTICE / CONTRIBUTING / SPDX.
- [ ] `SECURITY.md` + private vulnerability reporting + supported-versions.
- [ ] Full git-history secret scan. Rotate anything ever committed.
- [ ] Update the frontend deps that have a dev-server advisory (Vite / esbuild).

> If it must go public before all of the above close:
>
> 1. Mark it **experimental / not production-ready**.
> 2. Ship RW/DDL disabled and auto-approval off by default.
> 3. Include a Known-Limitations / security-boundaries doc.

## P1 — Public alpha: runnable by anyone, no mandatory vendor

**Try it in five minutes (packaging)**
- [ ] `docker compose up` → app + metadata Postgres + seeded demo target,
      auto-migrated, with a guarded demo login (no Slack, no cloud).
- [ ] `.env.example` + documented config precedence. Sane local defaults.
- [ ] README overhaul: web-IDE screenshot / demo GIF, 60-second quickstart,
      architecture diagram, capability matrix (mark ClickHouse experimental /
      fail-closed).
- [ ] Release engineering: semver tags + GitHub Releases + generated
      `CHANGELOG.md`.
- [ ] CI beyond unit tests: Postgres integration, fresh + upgrade migration
      runs, SQL Server smoke, frontend build + audit. Actions pinned to SHAs,
      and minimal workflow token permissions.
- [ ] Health/readiness endpoint + a minimal operator log/metric story.

**Decouple the mandatory dependencies (ports)**
- [ ] Approval + notify port: first-class web-only approval (queue + bell, Slack
      absent), and a `NotifyChannel` interface (web default, with Slack / email
      / webhook adapters). Every Slack call is a no-op when Slack is
      unconfigured.
- [ ] Result-delivery port (in-app download as the default, Slack upload as an
      adapter), so that execution depends on no chat vendor.
- [ ] Artifact-storage port (local FS as the default, plus S3 / GCS / Azure).
      The result directory is config, not a constant.
- [ ] Identity port: generic OIDC + reverse-proxy-header providers alongside
      Slack OIDC, and a documented Slack-free login.
- [ ] Packaging: move `slack-*` to a `[slack]` extra (matching the SQL Server
      extra). The base install pulls only what the vanilla profile needs.

**Web, auth & transport hardening**
- [ ] Production guard: HTTPS base URL, secure cookies, explicit trusted proxy.
- [x] The Slack sign-in's workspace check fails closed when it cannot establish
      the workspace. `web_slack_team_id` pins it (Unreleased).
- [ ] CSRF token / strict Origin on state-changing routes. Security headers
      (CSP, HSTS, frame-ancestors, …). WebSocket origin check.
- [ ] Trusted-proxy-aware client IP for audit (do not trust a raw forwarded
      header). *(Applies directly to direct-IP deployments.)*
- [x] Session-secret minimum length / entropy check.
- [x] Separate metadata roles: owner / migrator / runtime / audit-writer. The
      runtime role cannot mutate the audit trail. The app does not run
      migrations. CI exercises `scripts/split_metadata_roles.py`
      (docs/OPERATIONS.md §28).
- [x] Transport identity: PostgreSQL `verify-full` + per-target CA, and hostname
      checks on SQL Server certificates. Per-host lists
      (`target_ssl_verify_hosts`). Enabling it is a rollout step
      (docs/OPERATIONS.md §27).
- [ ] Ship the built frontend as the production artifact. Disable the raw
      CDN/prototype fallback, unless an explicit dev flag is set.

## P2 — Public beta: flexible & production-grade

**Multi-engine data sources**
- [ ] Engine adapter contract (connect, tier-matched execute, cancel/timeout,
      row/byte limits, result shaping) + a per-engine conformance test suite.
- [ ] MySQL / MariaDB adapter. SQL Server promoted to first-class.
- [x] ClickHouse and Amazon Athena, read-only (1.0.33).
- [x] Read-only PostgreSQL queries on a healthy read replica, off by default
      (1.0.33).
- [ ] Per-engine dialect-aware safety (use `sqlglot` dialects), so that
      RO/RW/DDL tiering is right for each engine.
- [ ] Later: warehouse read connectors (Snowflake / BigQuery / Redshift).

**Reliability at scale**
- [ ] Distributed work queue. Per-target and per-tier concurrency budgets + a
      fleet-wide budget. Backpressure + queue-depth metrics.
- [ ] Heartbeat + lease + orphan recovery (liveness-based, not "age of
      `executed_at`"), so that recovery never mistakes a long query for a dead
      one.
- [ ] Bounded server-side cursors + per-cell/per-row byte caps in the executor's
      export path (do not materialize huge results client-side).

**Secrets & credentials**
- [ ] Pluggable credential provider: local encrypted vault (default) / AWS
      Secrets Manager / HashiCorp Vault / env. Each install chooses one. No
      cloud is required.
- [x] Master-key rotation. The key file is a ring (primary first, older keys
      still decrypt). It comes with `scripts/rotate_master_key.py` for the
      online re-encryption pass, and with
      [docs/KEY_ROTATION.md](docs/KEY_ROTATION.md). No active-key id: trying the
      primary discovers which key wrote a value. That keeps the ciphertext
      format unchanged and the pass resumable.
- [ ] Optional dynamic, short-TTL credentials (Vault / cloud IAM) toward
      zero-standing-privilege.

**Audit & observability**
- [ ] Immutable audit: runtime INSERT-only, hash-chain, external append-only
      sink (WORM / SIEM) + a verification CLI.
- [ ] OpenTelemetry spans across submit → approve → execute → deliver
      (sanitized query summary, and raw SQL never a default telemetry
      attribute). Metrics and structured logs shipped: see "closed" above.
      Tracing is what remains, and it is the part that needs a dependency.
- [ ] Retention + redaction/tokenization for SQL, literals and justifications.

**Approval depth & authorization clarity**
- [ ] Risk-based approval routing: N-of-M, condition-based, JIT time-bound
      grants.
- [ ] Split the "bypass visibility" grant from privilege tier / database scope.
      Resolve ambiguous multi-team role selection deterministically (fail-closed
      on conflict).

**Supply chain**
- [x] Dependency lock / constraints + Dependabot/Renovate (pip, npm, Actions).
      The image installs a hash-locked set. The release audits it.
- [ ] CodeQL + secret scanning in CI (blocking for release).
- [ ] SBOM + signed releases + build provenance (SLSA). Artifact/container
      smoke test.

## P3 — v1.0: differentiation & long tail

- [ ] **Query Authorization Envelope (QAE).** A signed manifest that binds the
      *approved intent* to execution. The approved intent is SQL + AST hash,
      target, database, role, resource limits, plan budget, data policy, quorum
      and expiry. The worker refuses to run on any drift or after expiry. This
      turns "what did the human approve?" into a machine-verifiable contract.
      It is the candidate to standardize. It depends on the P0 trust chain and
      the P2 immutable audit.
- [ ] **Policy-as-code.** An OPA/Cedar adapter that separates the policy
      *decision* from enforcement (structured input → allow / required-approvals
      / effective-limits).
- [ ] **Plan-budget routing.** Estimated rows/cost/plan-shape steer the approval
      tier. This never replaces database-side timeouts and resource governors.
- [ ] **Schema-aware review + blast-radius estimator** (locks, affected rows,
      WAL, replica impact, online-DDL capability).
- [ ] **Metadata-store SQLite option** for single-node / evaluation installs.
- [ ] **White-label branding** admin (name, logo, accent) on the theme-token
      system. **PII region packs** (generic + country-specific, selectable).
      **i18n**. **Helm chart**.
- [ ] **Masking-exemption admin screen.** `pii_masking_exemptions` is
      psql-only today: 31 rows, written by hand, and every one of them widens
      what a result shows. The screen has to make the semantics legible rather
      than expose the columns:
  - Scope is a ladder (target / database / schema / table / column), and each
    rung reaches further.
  - `keep_value_scan` separates "stop matching this column by NAME" from "stop
    looking at the values".
  - `apply_in_joins` decides whether the exemption survives a query that also
    touches a masked table.

  Those three are the ones a person gets wrong from a form. `reason` is not a
  note: it is what an auditor reads later, so it belongs in the flow rather than
  beside it. The screen needs a preview against a real recent query on that
  table. The honest question is "what will people see that they do not see
  now". The current answer takes a psql session and knowledge of which of two
  resolver functions applies.
- [ ] Position PII masking honestly in docs as accidental-exposure mitigation,
      not a hard data boundary. That boundary lives in column privilege / RLS /
      masking views on the target.
- [ ] **Plugin surface** for custom detectors, safety policies and notification
      channels. **Multi-tenancy** if demand warrants.
- [ ] **Agent access (MCP server).** Expose submit / status / result as an MCP
      tool surface. Then a coding agent asks QueryHub for data, instead of
      receiving a production credential. The point is that nobody has to invent
      anything new for it. An agent is just another principal that should not
      hold a credential. Per-statement classification plus human approval is
      already the primitive that makes its access safe to grant. It requires:
  - a principal kind for non-human callers (`agent:<name>`)
  - a hard auto-approve ceiling of RO for them, regardless of grant
  - per-agent rate limits
  - the agent's prompt/justification, recorded in `audit_log` alongside the SQL

  It is deliberately *not* a natural-language-to-SQL feature. The agent writes
  the SQL, and QueryHub governs it.

---

## Principles / non-goals

- **Sane defaults over knobs.** A newcomer configures nothing to see it work.
  Power comes from optional adapters, not required ones.
- **No mandatory network egress.** The vanilla profile talks only to its own
  database and the targets you point it at.
- **Security is not obscurity, but disclosure is coordinated.** The defenses are
  open by design. The project handles specific unfixed weaknesses privately
  until they are fixed (see `SECURITY.md`). It never publishes them as a how-to.
- **The audit trail is not optional.** QueryHub records every decision and
  execution. No flag ever disables that invariant.
- **Not** an ORM, a BI tool, or a general query IDE for end users. It is a
  governed, audited path to production data.

> This document is the product backlog. It merges an architecture plan
> (ports & adapters) with a security / open-source-readiness review. Where a
> reviewed finding may already be partly handled, its item says *check*.
