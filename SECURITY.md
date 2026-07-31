# Security Policy

QueryHub is a security boundary — an admin-approved, audited gateway between
developers and production databases. We take reports seriously and appreciate
responsible disclosure.

## Supported versions

QueryHub is pre-1.0 and evolving quickly. Security fixes land on the latest
`main` and the most recent tagged release. Please reproduce on an up-to-date
checkout before reporting.

| Version | Supported |
|---------|-----------|
| latest `main` | ✅ |
| most recent tagged release | ✅ |
| older releases | ❌ — no backports; upgrade forward |

This is deliberately narrow and it matches
[README.md](README.md#versioning-and-support): one maintainer cannot honestly
offer an LTS, so none is offered. If you pin a release, plan to move forward for
security fixes.

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use GitHub's private
vulnerability reporting on this repository:

> **Security** tab → **Report a vulnerability**

Please include: affected component, a reproduction (SQL / request / config),
the impact you observed, and the commit or version you tested. We aim to
acknowledge within a few business days and to agree a coordinated disclosure
timeline with you.

## In scope

- SQL safety bypass — running a statement at a tier it shouldn't (RO/RW/DDL
  classification bypass, multi-statement smuggling, `SET`-based limit evasion).
- Authorization bypass — acting outside a grant (wrong target/database/tier),
  approval bypass, or privilege escalation.
- Cross-database / cross-catalog access beyond the granted scope.
- Credential / secret exposure (target credentials, master key, session
  secrets, tokens).
- Result-path attacks — spreadsheet formula injection, PII leaking past the
  masking layer in an exported result.
- Audit tampering — mutating or forging the audit trail from the runtime role.
- Web auth issues — session handling, CSRF, workspace/tenant confusion.

## Out of scope

- A **target database's own** misconfigured privileges (e.g. a role that has
  more rights than intended). QueryHub enforces its policy on top of the
  database's; it can't grant less than the credentials it's given already deny.
  Harden the target roles per the deploy guide.
- Denial of service from a single expensive-but-authorized query (bounded by
  `statement_timeout`, row/byte caps) — tune those limits for your fleet.
- Findings that require an already-compromised host or an already-privileged
  admin account.
- Missing hardening that is tracked openly in `ROADMAP.md`.

## Repository hardening (maintainers)

The application ships hardened, but the repository's protections are a
maintainer setting, not code — enable them on the hosting side:

- **Branch protection** on the default branch: require pull-request review,
  passing CI, and up-to-date branches before merge; disallow force-push and
  deletion.
- **Require signed commits** (this project signs its commits; enforce
  "Verified" on protected branches).
- **Secret scanning + push protection** (GitHub Advanced Security or the free
  equivalent) so a credential can't be pushed.
- **Dependabot / dependency alerts** for the pinned Python and npm deps.

These are one-time settings in the repository's Settings → Branches / Security
pages and are not enforceable from within the codebase.

## Design note

QueryHub's defenses are open source by design — security comes from
correctness and defense-in-depth (two-pass SQL analysis, tier-matched
credentials, per-value PII masking, immutable audit), not from secrecy. Specific
*unfixed* weaknesses are handled privately with the reporter until a fix ships;
they are never published as a how-to.
