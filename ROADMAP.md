# QueryHub Roadmap

QueryHub is an admin-approved, fully-audited SQL gateway you can self-host:
developers submit SQL, an admin approves, the gateway executes with
tier-matched (RO / RW / DDL) credentials, masks PII in the result, and audits
every step.

This roadmap makes one promise concrete: **QueryHub should run for anyone, on
anything, with no mandatory vendor** — not tied to a specific cloud, not tied to
Slack, not tied to a single database engine. You bring a database and a way to
sign in; everything else is optional and swappable. And because a SQL gateway is
a *security boundary*, it is held to a higher bar than an ordinary app: the
trust chain from "what was approved" to "what actually ran" must be durable,
re-checked, and impossible to widen from the query itself.

---

## Design principle — ports & adapters

The core pipeline is small and fixed:

```
submit → classify (RO/RW/DDL) → approve → execute → deliver → audit
```

The core depends only on **interfaces (ports)**. Every external concern is an
**adapter** selected by configuration, and every port ships a **zero-dependency
default** so a fresh install works with nothing but a database.

| Port | What it does | Default (zero-dep) | Optional adapters |
|------|--------------|--------------------|-------------------|
| **Identity** | who is signing in | Local users / generic OIDC | Slack OIDC, SAML, reverse-proxy header, email magic-link |
| **Approval + notify** | fan-out to approvers, collect the decision, tell the requester | Web admin panel + in-app notification bell | Slack, email (SMTP), webhook, Microsoft Teams |
| **Secrets** | target DB credentials at rest / just-in-time | Local encrypted vault (Fernet) | AWS Secrets Manager, HashiCorp Vault, dynamic (IAM), env vars |
| **Engine** | the data source being queried | PostgreSQL | MySQL, SQL Server, ClickHouse, (Snowflake / BigQuery / Redshift later) |
| **Artifacts** | where result files live | Local filesystem | S3, GCS, Azure Blob |
| **Metadata store** | QueryHub's own control-plane data | PostgreSQL | SQLite (small / single-node) |

### The "vanilla" profile — zero silly dependencies

The out-of-the-box install a newcomer gets:

> PostgreSQL metadata + a PostgreSQL target + local encrypted vault +
> **web-only** approval (no Slack) + local-filesystem results + email/none
> notifications — brought up with a single `docker compose up`.

No cloud account. No Slack workspace. No message broker. Slack, AWS, and every
other integration are strictly opt-in.

---

## Security & correctness invariants (the trust chain)

These are the properties every release is measured against. They are goals the
codebase moves toward, not claims about today — the phases below are how they
get met. None of them is ever put behind a convenience flag.

- **Durable, single, atomic execution.** An approved job survives a process
  crash and runs *exactly once* — dispatch is backed by a queue/outbox, and a
  worker claims a job with a compare-and-set (only an un-run job can be
  started), never a best-effort in-memory hand-off.
- **Re-authorized at execution.** Grants, membership, allowed databases and
  policy are re-checked at run time, not just at submit — a revoked grant stops
  a queued or scheduled job.
- **Resource + scope limits the query cannot widen.** Statement timeout, memory,
  row/byte caps and database/schema scope are enforced by the executor and the
  target's own privileges; user SQL cannot raise a limit or reach another
  database, catalog, or server.
- **Safe-by-default approval.** Write and schema changes need a second party by
  default; any time-bounded auto-approval is parameterized (bounded inputs), not
  literal-blind pattern matching.
- **Tamper-evident, immutable audit.** The runtime role cannot rewrite the audit
  trail; append-only with a hash-chain / external WORM-or-SIEM sink option.
- **Least privilege everywhere.** The metadata runtime role is not the schema
  owner; target credentials are scoped to what a tier needs, not blanket
  fleet-wide roles.

---

## Current state (honest baseline)

Already decoupled or close:

- **Approval engine is channel-free.** `core_submit` / `core_decide` are
  transport-agnostic; the web admin panel already approves/rejects without
  Slack. Slack is a lazy import used only for notification delivery.
- **Engine dispatch exists** and fails closed on an engine it can't run —
  Postgres is the default, not a hard-wired assumption; a SQL Server path exists
  behind an extra.
- **No cloud in the hot path.** All cloud/provisioning code lives in operator
  scripts; no cloud SDK is a core dependency.
- **SQL safety is two-pass** (keyword allow-list + a `sqlglot` AST pass) and
  dialect-capable.

Honest caveats to close:

- Result **delivery** and the result **path** are not yet ports.
- The metadata store is PostgreSQL-specific.
- Type checking is not yet clean/blocking (`mypy` runs advisory in CI).
- **One process, no HA.** The scheduler and boot recovery run in a single
  process — dispatch is `SKIP LOCKED`-safe but boot recovery is not, so a
  second web replica in the vanilla profile would double-run it. The login
  throttle is per-process for the same reason. This is the largest
  architectural limit; see [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

Closed since this section was written — kept visible so the list stays
falsifiable rather than flattering:

- ~~"300+ tests, mostly mocked, no real DB in CI"~~ → **989 tests**, plus a CI
  job that runs real-DB integration tests against a Postgres service container
  and fails if they skip.
- ~~"Slack is still a core packaging dependency"~~ → it is the `[slack]` extra;
  a `vanilla-import` CI job proves the base install imports the web, core and
  executor without it.
- ~~"no container yet"~~ → `Dockerfile` + `docker-compose.yml` bring up the app,
  its metadata database and a seeded demo target; a CI job drives a full
  submit → approve → execute round trip against it.
- ~~"Dependabot/Renovate" (was P2)~~ → `.github/dependabot.yml` is in place.
- ~~"PII region packs" (was P3)~~ → the pack mechanism exists (`pii_region`,
  generic + one region). What remains is *more* packs, not the mechanism.
- ~~"No operational telemetry: no metrics endpoint, no structured logs"~~ →
  `GET /metrics` in Prometheus text format (off by default, bearer token or
  admin session) and `LOG_FORMAT=json`. Values are computed from SQL at scrape
  time, so they survive restarts and do not differ between the two processes.
  No quantiles — see [docs/OPERATIONS.md](docs/OPERATIONS.md#24-monitoring-metrics-and-structured-logs).
- ~~"One encryption key, no rotation tooling"~~ → the master key file is a ring
  (line 1 is the primary, older lines still decrypt), so a new key can be
  introduced without downtime; `scripts/rotate_master_key.py` does the
  re-encryption pass — dry-run by default, one transaction, round-trip verified,
  resumable — and [docs/KEY_ROTATION.md](docs/KEY_ROTATION.md) is the procedure.

---

## Phase milestones

| Phase | Milestone | Theme |
|-------|-----------|-------|
| **P0** | publish-safe | security + correctness + legal blockers to close before the repo is public |
| **P1** | public alpha | anyone can run it (packaging) + no mandatory vendor (decoupling) + web/auth/transport hardening |
| **P2** | public beta / production-grade | multi-engine, reliability at scale, pluggable secrets, immutable audit, observability |
| **P3** | v1.0 | differentiation: signed execution contract, policy-as-code, and the productization long tail |

Phases are ordered by leverage and safety, not locked in lockstep.

---

## P0 — Publish-safe gate

Close before the repository goes public or before any "try me" artifact ships.
(Detailed, reproduction-level security notes are tracked privately, not here.)

**Execution trust chain**
- [ ] Enforce server-side resource limits that a query cannot override (timeout,
      memory, planner knobs are validated by value and range, not just by name).
- [ ] Durable execution: queue/outbox + atomic compare-and-set claim so an
      approved job cannot be lost on crash or run twice. *(Verify against the
      existing execution-lease table — part of this may already hold.)*
- [ ] Re-authorize at execution time (grant / enabled / allowed-database /
      policy re-checked after the job is claimed).

**Scope safety**
- [ ] Harden PostgreSQL object resolution (`pg_catalog`-first search path;
      flag writable-schema targets at onboarding).
- [ ] Confine SQL Server to the selected database — reject cross-catalog and
      linked-server identifiers unless explicitly allow-listed per target.
- [ ] Persist the engine + required tier on the request and revalidate them at
      approval so tier classification is always engine-correct.

**Result safety**
- [ ] Neutralize spreadsheet formula injection in CSV/XLSX export (after
      masking).
- [ ] Fix large-result XLSX export. *(Reproduce first to confirm the exact
      boundary.)*

**Approval defaults**
- [ ] Require a second approver for RW/DDL by default; self-approval off by
      default with an audited break-glass path.
- [ ] Auto-approval off by default; replace literal-blind fingerprints with
      admin-approved parameterized templates (bounded inputs).

**Release hygiene**
- [ ] Migration runner: applied-version ledger + checksum + advisory lock +
      dirty-state handling. *(Verify current behavior — the runner already
      appears to track applied migrations; confirm what's missing.)*
- [ ] Single, consistent license across LICENSE / NOTICE / CONTRIBUTING / SPDX.
- [ ] `SECURITY.md` + private vulnerability reporting + supported-versions.
- [ ] Full git-history secret scan; rotate anything ever committed.
- [ ] Update dev-server-advisory frontend deps (Vite / esbuild).

> If it must go public before all of the above close: mark it **experimental /
> not production-ready**, ship RW/DDL disabled and auto-approval off by default,
> and include a Known-Limitations / security-boundaries doc.

## P1 — Public alpha: runnable by anyone, no mandatory vendor

**Try it in five minutes (packaging)**
- [ ] `docker compose up` → app + metadata Postgres + seeded demo target,
      auto-migrated, with a guarded demo login (no Slack, no cloud).
- [ ] `.env.example` + documented config precedence; sane local defaults.
- [ ] README overhaul: web-IDE screenshot / demo GIF, 60-second quickstart,
      architecture diagram, capability matrix (mark ClickHouse experimental /
      fail-closed).
- [ ] Release engineering: semver tags + GitHub Releases + generated
      `CHANGELOG.md`.
- [ ] CI beyond unit tests: Postgres integration, fresh + upgrade migration
      runs, SQL Server smoke, frontend build + audit; Actions pinned to SHAs,
      minimal workflow token permissions.
- [ ] Health/readiness endpoint + a minimal operator log/metric story.

**Decouple the mandatory dependencies (ports)**
- [ ] Approval + notify port: first-class web-only approval (queue + bell, Slack
      absent) and a `NotifyChannel` interface (web default; Slack / email /
      webhook adapters); every Slack call a no-op when unconfigured.
- [ ] Result-delivery port (in-app download default; Slack upload an adapter) so
      execution depends on no chat vendor.
- [ ] Artifact-storage port (local FS default; S3 / GCS / Azure); result
      directory is config, not a constant.
- [ ] Identity port: generic OIDC + reverse-proxy-header providers alongside
      Slack OIDC; a documented Slack-free login.
- [ ] Packaging: move `slack-*` to a `[slack]` extra (matching the SQL Server
      extra); the base install pulls only what the vanilla profile needs.

**Web, auth & transport hardening**
- [ ] Production guard: HTTPS base URL, secure cookies, explicit trusted proxy,
      mandatory identity-provider workspace/tenant id (fail-closed on lookup
      failure).
- [ ] CSRF token / strict Origin on state-changing routes; security headers
      (CSP, HSTS, frame-ancestors, …); WebSocket origin check.
- [ ] Trusted-proxy-aware client IP for audit (don't trust a raw forwarded
      header). *(Applies directly to direct-IP deployments.)*
- [ ] Session-secret minimum length / entropy check.
- [ ] Separate metadata roles: owner / migrator / runtime / audit-writer — the
      runtime role cannot mutate the audit trail; the app does not run
      migrations.
- [ ] Transport identity: PostgreSQL `verify-full` + per-target CA; SQL Server
      certificate hostname validation.
- [ ] Ship the built frontend as the production artifact; disable the raw
      CDN/prototype fallback unless an explicit dev flag is set.

## P2 — Public beta: flexible & production-grade

**Multi-engine data sources**
- [ ] Engine adapter contract (connect, tier-matched execute, cancel/timeout,
      row/byte limits, result shaping) + a per-engine conformance test suite.
- [ ] MySQL / MariaDB adapter; SQL Server promoted to first-class; ClickHouse
      (read-oriented).
- [ ] Per-engine dialect-aware safety (leverage `sqlglot` dialects) so RO/RW/DDL
      tiering is correct per engine.
- [ ] Later: warehouse read connectors (Snowflake / BigQuery / Redshift).

**Reliability at scale**
- [ ] Distributed work queue; per-target and per-tier concurrency budgets + a
      fleet-wide budget; backpressure + queue-depth metrics.
- [ ] Heartbeat + lease + orphan recovery (liveness-based, not "age of
      `executed_at`") so a long query is never mistaken for a dead one.
- [ ] Bounded server-side cursors + per-cell/per-row byte caps in the executor's
      export path (don't materialize huge results client-side).

**Secrets & credentials**
- [ ] Pluggable credential provider: local encrypted vault (default) / AWS
      Secrets Manager / HashiCorp Vault / env — chosen per install, no cloud
      required.
- [x] Master-key rotation — the key file is a ring (primary first, older keys
      still decrypt), plus `scripts/rotate_master_key.py` for the online
      re-encryption pass and [docs/KEY_ROTATION.md](docs/KEY_ROTATION.md).
      No active-key id: which key wrote a value is discovered by trying the
      primary, which keeps the ciphertext format unchanged and the pass
      resumable.
- [ ] Optional dynamic, short-TTL credentials (Vault / cloud IAM) toward
      zero-standing-privilege.

**Audit & observability**
- [ ] Immutable audit: runtime INSERT-only, hash-chain, external append-only
      sink (WORM / SIEM) + a verification CLI.
- [ ] OpenTelemetry spans across submit → approve → execute → deliver
      (sanitized query summary; raw SQL never a default telemetry attribute).
      Metrics and structured logs shipped — see "closed" above; tracing is
      what remains, and it is the part that needs a dependency.
- [ ] Retention + redaction/tokenization for SQL, literals and justifications.

**Approval depth & authorization clarity**
- [ ] Risk-based approval routing: N-of-M, condition-based, JIT time-bound
      grants.
- [ ] Split the "bypass visibility" grant from privilege tier / database scope;
      resolve ambiguous multi-team role selection deterministically (fail-closed
      on conflict).

**Supply chain**
- [ ] Dependency lock / constraints + Dependabot/Renovate (pip, npm, Actions).
- [ ] CodeQL + secret scanning in CI (blocking for release).
- [ ] SBOM + signed releases + build provenance (SLSA); artifact/container smoke
      test.

## P3 — v1.0: differentiation & long tail

- [ ] **Query Authorization Envelope (QAE).** A signed manifest that binds the
      *approved intent* — SQL + AST hash, target, database, role, resource
      limits, plan budget, data policy, quorum, expiry — to execution. The
      worker refuses to run on any drift or after expiry. This turns "what did
      the human approve?" into a machine-verifiable contract and is the
      candidate to standardize; it depends on the P0 trust chain and the P2
      immutable audit.
- [ ] **Policy-as-code.** An OPA/Cedar adapter that separates the policy
      *decision* from enforcement (structured input → allow / required-approvals
      / effective-limits).
- [ ] **Plan-budget routing.** Estimated rows/cost/plan-shape steer the approval
      tier — never a replacement for database-side timeouts and resource
      governors.
- [ ] **Schema-aware review + blast-radius estimator** (locks, affected rows,
      WAL, replica impact, online-DDL capability).
- [ ] **Metadata-store SQLite option** for single-node / evaluation installs.
- [ ] **White-label branding** admin (name, logo, accent) on the theme-token
      system; **PII region packs** (generic + country-specific, selectable);
      **i18n**; **Helm chart**.
- [ ] Position PII masking honestly in docs as accidental-exposure mitigation,
      not a hard data boundary (that lives in column privilege / RLS / masking
      views on the target).
- [ ] **Plugin surface** for custom detectors, safety policies and notification
      channels; **multi-tenancy** if demand warrants.
- [ ] **Agent access (MCP server).** Expose submit / status / result as an MCP
      tool surface, so a coding agent asks QueryHub for data instead of being
      handed a production credential. The point is that nothing new has to be
      invented for it: an agent is just another principal that should not hold a
      credential, and per-statement classification plus human approval is
      already the primitive that makes its access safe to grant. Requires: a
      principal kind for non-human callers (`agent:<name>`), a hard
      auto-approve ceiling of RO for them regardless of grant, per-agent rate
      limits, and the agent's prompt/justification recorded in `audit_log`
      alongside the SQL. Deliberately *not* a natural-language-to-SQL feature —
      the agent writes the SQL, QueryHub governs it.

---

## Principles / non-goals

- **Sane defaults over knobs.** A newcomer configures nothing to see it work;
  power comes from optional adapters, not required ones.
- **No mandatory network egress.** The vanilla profile talks only to its own
  database and the targets you point it at.
- **Security is not obscurity, but disclosure is coordinated.** The defenses are
  open by design; specific unfixed weaknesses are handled privately until fixed
  (see `SECURITY.md`), never published as a how-to.
- **The audit trail is not optional.** Every decision and execution is recorded
  — that invariant is never behind a flag.
- **Not** an ORM, a BI tool, or a general query IDE for end users; it is a
  governed, audited path to production data.

> This document is the product backlog. It merges an architecture plan
> (ports & adapters) with a security / open-source-readiness review; where a
> reviewed finding may already be partly handled it is marked *verify*.
