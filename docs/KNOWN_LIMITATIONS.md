# Known limitations

QueryHub is **experimental / early-stage**. Run it behind your own network
controls. Do not expose it to the internet.

QueryHub runs in production for its author, but the public project is young.
Expect minor defects and unfinished parts. This page is an honest inventory of
what is there and what is not there yet. The forward plan is in
[ROADMAP.md](../ROADMAP.md).

## Approval model

- **Single-operator by default.** The default configuration assumes one
  DBA/operator. By default, QueryHub does not enforce peer approval for
  writes, and a super-admin can self-approve. If you run with more than one
  approver, review the approval settings before you rely on them.
- **Approval + notifications are richest on Slack.** Web-only approval works
  (queue + in-app notifications). Work is in progress to generalize the
  notify/delivery paths into pluggable adapters (see the roadmap). For now,
  treat the non-Slack paths as less tested in real use.

## Engines

- **PostgreSQL is first-class.** QueryHub supports SQL Server (safety +
  execution), but it blocks cross-database / linked-server references.
  Amazon Athena and ClickHouse execute read-only (SELECT / WITH). An engine
  with a spec but no execution path fails closed.
- QueryHub blocks cross-database access on purpose. A query is scoped to the
  target database that it was submitted against.
- **Read replicas are PostgreSQL only, and off by default**
  (`replica_routing`). A replica read can be up to `replica_max_lag_seconds`
  behind. Read-your-writes covers writes made through QueryHub only, not
  writes the application makes. The check for reads that must stay on the
  primary (`pg_stat_*`, `pg_locks`, WAL functions ...) examines the SQL text.
  Because of this, it does not detect a view that wraps one of them. QueryHub
  does not route reads to SQL Server readable secondaries.

## Testing & typing

- The **fast** suite is pure-logic and hermetic by construction. A unit test
  that tries to open a real database connection fails with a named error. It
  does not hang.
- Separate tests cover real-DB behaviour. `tests/test_integration_db.py` runs
  against a throwaway Postgres 16 in CI. It tests claim exclusivity under
  concurrency, migration-ledger idempotency, and session rotation against real
  SQL. The CI job fails if those tests report as *skipped*. In the past, the
  tests reported as skipped and so never ran.
- Two areas still have thin coverage. SQL Server behaviour has no live
  coverage (no CI instance). The tests assert `SET`/`search_path`/role
  semantics on the issued statements, not on their effect.
- The codebase is **not fully typed**. `mypy` blocks CI only on errors that
  are not already in a committed baseline (`scripts/mypy_baseline.txt`). Only
  the session, login-provider and credential-provider modules must pass
  `--strict`. The newer modules type clean, and the safe/mechanical findings
  are fixed.
- The residual `mypy` errors are annotation gaps in the larger legacy modules
  (Slack handlers, the metrics/mapping builders, the auth-event outbox). In
  these modules, runtime guards already exist, but the checker can't narrow
  them. Most of the 242 errors are annotation gaps, not bugs, but not all of
  them. When the project enabled the strict flags for three modules, the
  flags found two real bugs (fixed 2026-09-25). The roughly 100 "may be None"
  errors are not audited yet.

## Install & packaging

- **`pip install` is not a supported install.** The wheel collects `src/`
  only, so it carries no migrations, no built frontend and no ops scripts.
  Also, those scripts resolve their paths relative to a checkout. For that
  reason, the project publishes nothing to PyPI.
- The supported install is the container (`docker-compose.install.yml`).
  `pip install -e .` from a clone is the contributor path. The fix is tracked
  packaging work, in this order:
  1. Ship the assets as package data.
  2. Make the repo-root path lookups resolve through the installed package.
  3. Expose the ops scripts as console entry points.
- The container image is **web-only** (the vanilla profile). To run the Slack
  surface, you need the `[slack]` extra and a second process. See
  [../deploy/INSTALL.md](../deploy/INSTALL.md). This applies to the published
  image and also to an image that you build.
- The install path leaves **TLS** and **backups of the metadata database** to
  you. The published port binds to loopback and expects a reverse proxy. The
  metadata database holds the audit log. So it needs the same backup and
  retention treatment as the databases that QueryHub fronts. The optional
  `bundled-db` compose profile puts the metadata database in a Docker volume.
  This is fine for an evaluation, but not fine for a deployment.

## Operations & scale

- The executor is **single-process**. There is no distributed queue or
  multi-node HA. Concurrency is bounded per process.
- Very large result exports stream to CSV/XLSX with row and size caps. You
  should narrow extreme result sets in SQL instead of exporting them
  wholesale.
- A checksum ledger tracks migrations. Never edit a committed migration in
  place. Add a new one.

## Security posture

- Hardening is ongoing. Several defense-in-depth items are still planned (see
  the roadmap). Do not treat the current state as a substitute for network
  isolation, least-privilege database roles, and your own audit.
- The local-login brute-force throttle is in-memory and per-process. This is
  right for the supported single-process deployment. But the counters reset
  on restart. If the web app ever scales to multiple processes, the processes
  do not share the counters. Move the counters to a DB table first.
- Local accounts **do** have self-service password change
  (`POST /api/auth/local/change-password`). QueryHub enforces
  `must_change_pw`: a router-level dependency blocks a flagged account from
  every action route until the account changes its password. A *reset* flow
  for a forgotten password is still missing. There is no email channel, so an
  operator re-runs `scripts/create_local_user.py` for that.
- Report vulnerabilities privately. See [SECURITY.md](../SECURITY.md). Do not
  open a public issue for a security problem.
