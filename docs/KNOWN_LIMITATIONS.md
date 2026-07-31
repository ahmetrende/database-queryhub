# Known limitations

QueryHub is **experimental / early-stage**. It runs in production for its
author, but the public project is young: expect rough edges, and run it
behind your own network controls rather than exposing it to the internet.
This page is an honest inventory of what is and isn't there yet. The
forward plan lives in [ROADMAP.md](../ROADMAP.md).

## Approval model

- **Single-operator by default.** The default configuration is built
  around one DBA/operator. Peer approval for writes is not enforced by
  default, and a super-admin can self-approve. If you run with more than
  one approver, review the approval settings before relying on them.
- **Approval + notifications are richest on Slack.** Web-only approval
  works (queue + in-app notifications), but the notify/delivery paths are
  being generalized into pluggable adapters (see the roadmap). Treat the
  non-Slack paths as less battle-tested for now.

## Engines

- **PostgreSQL is first-class.** SQL Server is supported (safety +
  execution), with cross-database / linked-server references blocked.
  ClickHouse currently ships only as a safety spec — there is no
  execution path yet, and an unwired engine fails closed.
- Cross-database access is intentionally blocked; a query is scoped to
  the target database it was submitted against.

## Testing & typing

- The **fast** suite (764 tests, ~7s) is pure-logic and hermetic by
  construction: a unit test that reaches for a real database connection fails
  with a named error rather than hanging. Real-DB behaviour is covered
  separately — `tests/test_integration_db.py` runs against a throwaway
  Postgres 16 in CI (claim exclusivity under concurrency, migration-ledger
  idempotency, session rotation against real SQL), and the CI job fails if
  those tests report as *skipped*, which is how they previously managed to
  never run. What is still thin: SQL Server behaviour has no live coverage
  (no CI instance), and `SET`/`search_path`/role semantics are asserted on the
  statements issued rather than on their effect.
- The codebase is **not fully typed**; `mypy` runs in CI as advisory, not
  as a blocking gate. The newer modules type clean and the safe/mechanical
  findings are fixed; the residual `mypy` errors are annotation gaps in the
  larger legacy modules (Slack handlers, the metrics/mapping builders,
  the auth-event outbox), where runtime guards already exist but the
  checker can't narrow them. Adding those annotations is a tracked,
  low-risk backlog — not a correctness bug.

## Install & packaging

- **`pip install` is not a supported install.** The wheel collects `src/`
  only, so it carries no migrations, no built frontend and no ops scripts,
  and those scripts resolve their paths relative to a checkout. Nothing is
  published to PyPI for that reason. The supported install is the container
  (`docker-compose.install.yml`); `pip install -e .` from a clone is the
  contributor path. Fixing this is tracked packaging work, in this order:
  ship the assets as package data, make the repo-root path lookups resolve
  through the installed package, then expose the ops scripts as console
  entry points.
- The container image is **web-only** (the vanilla profile). Running the
  Slack surface means the `[slack]` extra and a second process — see
  [../deploy/INSTALL.md](../deploy/INSTALL.md). This applies to the published
  image as much as to one you build.
- The install path leaves **TLS** and **backups of the metadata database**
  to you. The published port binds to loopback, expecting a reverse proxy;
  and the metadata database holds the audit log, so it wants the same
  backup and retention treatment as the databases QueryHub fronts. The
  optional `bundled-db` compose profile puts it in a Docker volume, which
  is fine for an evaluation and not fine for a deployment.

## Operations & scale

- The executor is **single-process**; there is no distributed queue or
  multi-node HA. Concurrency is bounded per process.
- Very large result exports stream to CSV/XLSX with row and size caps;
  extreme result sets should be narrowed in SQL rather than exported
  wholesale.
- Migrations are tracked by a checksum ledger; a committed migration must
  never be edited in place (add a new one).

## Security posture

- Hardening is ongoing. Several defense-in-depth items are still planned
  (see the roadmap). Do not treat the current state as a substitute for
  network isolation, least-privilege database roles, and your own audit.
- The local-login brute-force throttle is in-memory and per-process —
  right for the supported single-process deployment, but counters reset
  on restart and are not shared if the web app is ever scaled to multiple
  processes (move them to a DB table first).
- Local accounts **do** have self-service password change
  (`POST /api/auth/local/change-password`), and `must_change_pw` is enforced:
  a flagged account is blocked from every action route by a router-level
  dependency until it changes its password. What is still missing is a
  *reset* flow for a forgotten password — there is no email channel, so an
  operator re-runs `scripts/create_local_user.py` for that.
- Report vulnerabilities privately — see [SECURITY.md](../SECURITY.md).
  Do not open a public issue for a security problem.
