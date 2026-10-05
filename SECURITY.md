# Security Policy

QueryHub is a security boundary: an admin-approved, audited gateway between
developers and production databases. We take reports seriously. We appreciate
responsible disclosure.

## Supported versions

Security fixes land on the latest `main` and on the most recent tagged release.
QueryHub changes quickly. Please reproduce the problem on an
up-to-date checkout before you report it.

| Version | Supported |
|---------|-----------|
| latest `main` | ✅ |
| most recent tagged release | ✅ |
| older releases | ❌ — no backports. Upgrade forward. |

This policy is deliberately narrow, and it matches
[README.md](README.md#versioning-and-support). One maintainer cannot honestly
offer an LTS, so the project offers none. If you pin a release, plan to upgrade
for security fixes.

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use GitHub's private
vulnerability reporting on this repository:

> **Security** tab → **Report a vulnerability**

Please include these items:

- the affected component
- a reproduction (SQL / request / config)
- the impact that you observed
- the commit or version that you tested

We aim to acknowledge your report within a few business days. We also aim to
agree on a coordinated disclosure timeline with you.

## In scope

- SQL safety bypass — running a statement at a tier where it should not run
  (RO/RW/DDL classification bypass, multi-statement smuggling, `SET`-based limit
  evasion).
- Authorization bypass — acting outside a grant (wrong target/database/tier),
  approval bypass, or privilege escalation.
- Cross-database / cross-catalog access beyond the granted scope.
- Credential / secret exposure (target credentials, master key, session
  secrets, tokens).
- Result-path attacks — spreadsheet formula injection, or PII that leaks past
  the masking layer in an exported result.
- Audit tampering — mutating or forging the audit trail from the runtime role.
- Web auth issues — session handling, CSRF, workspace/tenant confusion.

## Out of scope

- A **target database's own** misconfigured privileges (e.g. a role that has
  more rights than you intended). QueryHub enforces its policy on top of the
  policy of the database. It cannot grant less than the credentials that it gets
  already deny. Harden the target roles as the deploy guide describes.
- Denial of service from a single expensive-but-authorized query.
  `statement_timeout` and the row/byte caps bound such a query. Tune those
  limits for your fleet.
- Findings that require an already-compromised host or an already-privileged
  admin account.
- Missing hardening that `ROADMAP.md` tracks openly.

## Repository hardening (maintainers)

Enable these protections on the hosting side. The application ships hardened,
but the protections of the repository are a maintainer setting, not code:

- **Branch protection** on the default branch: require pull-request review,
  passing CI, and up-to-date branches before merge. Disallow force-push and
  deletion.
- **Require signed commits.** This project signs its commits. Enforce
  "Verified" on protected branches.
- **Secret scanning + push protection** (GitHub Advanced Security or the free
  equivalent), so that nobody can push a credential.
- **Dependabot / dependency alerts** for the pinned Python and npm deps.

These are one-time settings on the Settings → Branches / Security pages of the
repository. The codebase cannot enforce them.

## Design note

QueryHub's defenses are open source by design. Security comes from correctness
and defense-in-depth, not from secrecy: two-pass SQL analysis, tier-matched
credentials, per-value PII masking, and an attributed audit trail. We handle
specific *unfixed* weaknesses privately with the reporter until a fix ships. We
never publish them as a how-to.
