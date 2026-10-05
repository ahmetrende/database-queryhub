# Audit v2 — triage of the remaining findings

The second audit produced 155 headed items. The 8 CRITICAL and 11 HIGH items
closed in their own rounds (M1–M6). This file is the working state of the other
136: 72 MEDIUM, 55 LOW and 9 recommendations. The file exists because "126 items
in a backlog" is not a state anyone can act on.

**Severities in the source document are claims, not facts.** They came from a
refuting agent that re-ran each finding. Spot checks gave mixed results in both
directions:

- One Slack-schema disclosure was real at database level and refuted at target
  level.
- The auditor's own verifier already refuted the "15 JSX modules have no
  imports" finding.
- Work on this file refuted two more, and showed one to be worse than described.

So each row below says how it was decided, and by what.

Each status below has one precise meaning:

| status | meaning |
|---|---|
| **fixed** | changed in this repository, with a test that fails if it regresses |
| **already** | an earlier round closed it. This pass re-checked it against the code |
| **refuted** | measured against the code, and the claim does not hold |
| **narrower** | the claim is partly right. The real defect is smaller or elsewhere |
| **open** | real, not done, with the reason it is not done |
| **operator** | needs a credential, an account or a decision only the operator has |

---

## Closed in this pass, with tests

| finding | status | what was actually true |
|---|---|---|
| `EXPLAIN ANALYSE` (British spelling) bypasses the ANALYZE gate | **refuted** | The gate matches `ANALY[SZ]E`, and it has done so since 2026-07-30. The gate blocks `EXPLAIN ANALYSE DELETE`, with the right message. |
| …"and the inner dangerous-function scan" | **narrower → fixed** | ANALYZE scans its inner statement. **Plain `EXPLAIN` did not**, so `EXPLAIN SELECT pg_read_file('/etc/passwd')` passed every gate, while the gates blocked the bare call. Plain EXPLAIN does not execute, so it never calls a VOLATILE function. But that is an argument about volatility. The planner DOES fold an IMMUTABLE function with constant arguments at plan time. Both forms now scan. |
| `SELECT ... INTO` classified `ro` | **fixed** | The claim holds on both engines. It creates a table. `CREATE TABLE AS`, the identical operation, was always `ddl`. Now it is `ddl`, with the depth walk that keeps a subquery's `INTO` from counting. |
| Disabling a target does not stop approved / queued / scheduled executions | **fixed** | The claim holds: `targets.get()` returns the row regardless, and the executor never looked. Now the executor refuses at execution time when the target is disabled, and names the target. |
| `check_repo_clean.py` fails open — an unreachable bot DB drops the whole dynamic denylist | **fixed** | The claim holds. ~250 dynamic patterns silently became 12 static ones, and the scan still printed "clean". Now the scan refuses. `--static-only` is the deliberate exception for a tree with no secrets. |
| Neither leak gate runs in CI or in a git hook | **fixed** | The claim holds. A `leak-gates` job now runs the scan. A second step proves that the scan still refuses when its denylist source is unreachable. |
| GitHub Actions pinned to floating tags, one to a mutable branch | **fixed** | The claim holds: 23 uses on tags, `pypa/gh-action-pypi-publish@release/v1` on a branch. The fix pins all of them to commit SHAs, with the resolved version in a comment. |
| No job in either workflow sets `timeout-minutes` | **fixed** | 10 jobs, default 6 hours. Now 20 minutes (CI) / 30 (release). |
| `ci.yml` declares no permissions | **fixed** | Now `contents: read` at workflow level. |
| The test suite and CI never execute any JavaScript | **fixed** | 29 frontend tests under `node --test`, which run in CI. Writing them found and fixed three real defects. |

*This pass* found three defects that the audit did not report:

- **`node --test test/` stopped working at node 20+.** So the CI step added the
  day before would have failed on node 25, while it passed locally on 18.
- **`_run` assigned `committed` inside its `try`**, and its `except` handler read
  the value. So anything that raised before that point crashed the error handler
  with an `UnboundLocalError`. That is the one place that must not fail. A new
  early return above it made the defect visible. The hazard was already there.
- **The new `leak-gates` job scanned all of history, not the pull request.** The
  message scan grandfathers upstream's pre-gate commits through a recorded SHA.
  That reasoning covered upstream (it has the anchor) and the published export
  (no shared commits, clean either way). It did not cover a downstream replica,
  which carries some of upstream's old commits and *not* the anchor. So the job
  re-flagged five 2026-05 messages that nobody in the pull request wrote. That
  repository's ruleset forbids the force push that would fix it. The job stayed
  permanently red, and that is how people start to bypass a gate. The job now
  derives the range from the event (`QH_SCAN_REV_RANGE`). The tests pin both
  directions: a leak *inside* the range still fails the build.

  The five messages themselves are a real, separate finding. Narrowing the range
  does not clean them. See **Needs an operator** below.

---

## Verified as already closed by an earlier round

This pass re-checked each item against the code, and took none on trust:

- json/jsonb, array and composite columns bypassing both masking layers (M3)
- The default column-name catalog mangling ordinary columns (M5)
- The SQL-safety verdict unenforced by tests at both enforcement points (M4)
- Commit messages bypassing every gate (M1). The attribution-trailer false
  positive that came from it is closed too.
- Five more:
  - README screenshots being design-mock renders (R2)
  - the falsified competitive claim (M6/F4.1)
  - LGPL third-party notices (`THIRD_PARTY_NOTICES.md`, enforced by the
    Dockerfile)
  - `QueryHubWeb/` excluded from the scanner (the scan covers it)
  - binary assets bypassing the gates (the export has a raster brand scan)

---

## Open, with the reason

These are real and not done. The reason matters more than the count.

**Needs a design decision, not an implementation**

- No partitioning on `audit_log` / `requests`, `web_sessions` never pruned, and
  the full rewrite of `schema_catalog` every hour. All three are the same
  question: what the retention and growth model is. Answering it per table
  without deciding the model produces three inconsistent answers.
- Single-process executor, in-memory throttle, hardcoded pool of 4. These are
  one decision: whether the executor is allowed to be multi-process. That
  decision changes the claim/lease design that B6 just settled.
- No tenancy dimension anywhere in the model. This one is correctly listed.
  Adding one is a schema-wide change, not a fix.

**Needs an operator, not a commit**

- The README's primary install path pulls a container image that is still
  unpublished (GHCR release).
- Dependabot alerts are DISABLED on the public repository (measured 2026-08-08).
- Retention and maintenance jobs ship as documentation. Nothing schedules them
  in the container. Wiring them needs a decision about whether the image runs a
  scheduler at all.
- ~~**Five 2026-05 commit messages carry what the gate forbids**~~. **Decided
  2026-08-13: grandfathered downstream too.** Three name a colleague. Two hold
  an operator-specific absolute path. Upstream already exempts them by an
  explicit cost decision. The cost: 391 of 449 commits, nine branches and three
  tags to rewrite, for a repository that is never published. The replica
  inherits the same commits. The alternative was a one-off force push, which the
  org ruleset forbids by default. What settled it: **the public repository does
  not contain them at all**. Its history is a separate root of 21 commits,
  verified by SHA. So the residual exposure is a colleague's first name, visible
  to colleagues at the same company, inside a private repository. That is not a
  leak, and the rewrite buys nothing against it. The gate now scans what each
  event introduces, so nothing new can join them.

**Real, small, not yet done**

Each item is a contained change with a test, so this file groups them. The batch
is the next obvious piece of work:

- Migration runner inherits the app pool's 10s `statement_timeout`. Per-file
  transactions make `CREATE INDEX CONCURRENTLY` structurally impossible.
- `install.sh` never creates `/var/lib/queryhub` or `/var/log/queryhub`. It also
  points the unit at TLS files that it may not have created.
- A control-DB blip at boot leaves the web process broken while `/healthz`
  reports 200
- Master key briefly world-readable between creation and `chmod`
- Two production connections hardcode `sslmode="require"`. This silently
  downgrades an operator who configured `verify-full`.
- Metadata-DB connections set no `sslmode` and expose no knob
- `routes_avatar`'s SSRF defence checks the status code after urllib already
  followed the redirect
- `database_name`, which the requester controls, is unvalidated free text, and
  it lands unescaped in the approval card
- The CSV-import table probe DMs the raw driver exception and bypasses
  `errors.scrub`
- Scoped ('dba') admins can read the whole fleet's audit trail, including SQL
  text for targets outside their scope
- The signing secret for web sessions derives from the raw master-key FILE, not
  from the parsed key ring. So any edit to the file invalidates every live
  session.
- Set-operation arms 2..n are invisible to the source-column resolver. This is
  adjacent to the EXPLAIN-lineage work, and it may already be covered. It needs
  measuring, not assuming.

**Documentation contradictions**

These form a cluster. One pass should fix them together: it reconciles the
documents against the code once, rather than one at a time.

- `ARCHITECTURE.md`'s transport-agnostic claim
- `DISASTER_RECOVERY.md` vs `KEY_ROTATION.md`
- `OPERATIONS.md` §4 vs the migration runner
- `SCHEMA.md`'s migration history, which stops at 054
- the three different descriptions of `kill_switch`
- `FEATURES.md`, which predates the vanilla profile
- the stale test counts in `KNOWN_LIMITATIONS.md` and `ROADMAP.md`

**Strategy items**

The positioning work is a set of decisions for the maintainer, not code:

- rewrite the competitive section around "the statement picks the credential"
- demote "nothing in your data path"
- file six good-first-issues
- the v0.2 MySQL-first plan
- the explicit do-not-build list

This file records them so that they stop counting as engineering backlog.

---

## How to keep this honest

When an item moves:

1. Move it in this file, in the same commit.
2. Say which of the six statuses it moved to.
3. Say what you measured.

An entry that says "fixed" with no test will be wrong within a month.
