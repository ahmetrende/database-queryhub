# Audit v2 — triage of the remaining findings

The second audit produced 155 headed items. The 8 CRITICAL and 11 HIGH were
closed in their own rounds (M1–M6). This file is the working state of the other
136 — 72 MEDIUM, 55 LOW and 9 recommendations — because "126 items in a backlog"
is not a state anyone can act on.

**Severities in the source document are claims, not facts.** They came from a
refuting agent that re-ran each finding, and spot checks have come out mixed in
both directions: one Slack-schema disclosure was real at database level and
refuted at target level; the "15 JSX modules have no imports" finding was already
refuted by the auditor's own verifier. Two more were refuted while writing this
file, and one turned out to be worse than described. So each row below says how it
was decided, and by what.

Status vocabulary, used precisely:

| status | meaning |
|---|---|
| **fixed** | changed in this repository, with a test that fails if it regresses |
| **already** | was closed by an earlier round; re-checked here against the code |
| **refuted** | measured against the code and the claim does not hold |
| **narrower** | the claim is partly right; the real defect is smaller or elsewhere |
| **open** | real, not done, with the reason it is not done |
| **operator** | needs a credential, an account or a decision only the operator has |

---

## Closed in this pass, with tests

| finding | status | what was actually true |
|---|---|---|
| `EXPLAIN ANALYSE` (British spelling) bypasses the ANALYZE gate | **refuted** | The gate matches `ANALY[SZ]E` and has since 2026-07-30. `EXPLAIN ANALYSE DELETE` is blocked, with the right message. |
| …"and the inner dangerous-function scan" | **narrower → fixed** | ANALYZE scans its inner statement. **Plain `EXPLAIN` did not**, so `EXPLAIN SELECT pg_read_file('/etc/passwd')` passed every gate while the bare call was blocked. Plain EXPLAIN does not execute, so a VOLATILE function is never called — but that is an argument about volatility, and an IMMUTABLE function with constant arguments IS folded at plan time. Both forms now scan. |
| `SELECT ... INTO` classified `ro` | **fixed** | Confirmed on both engines. It creates a table; `CREATE TABLE AS`, the identical operation, was always `ddl`. Now `ddl`, with the depth walk that keeps a subquery's `INTO` from counting. |
| Disabling a target does not stop approved / queued / scheduled executions | **fixed** | Confirmed: `targets.get()` returns the row regardless and the executor never looked. Disabling now refuses at execution time, naming the target. |
| `check_repo_clean.py` fails open — an unreachable bot DB drops the whole dynamic denylist | **fixed** | Confirmed. ~250 dynamic patterns silently became 12 static ones and the scan still printed "clean". Now a refusal; `--static-only` is the deliberate exception for a tree with no secrets. |
| Neither leak gate is wired into CI or a git hook | **fixed** | Confirmed. A `leak-gates` job now runs the scan, and a second step proves the scan still refuses when its denylist source is unreachable. |
| GitHub Actions pinned to floating tags, one to a mutable branch | **fixed** | Confirmed: 23 uses on tags, `pypa/gh-action-pypi-publish@release/v1` on a branch. All pinned to commit SHAs with the resolved version in a comment. |
| No job in either workflow sets `timeout-minutes` | **fixed** | 10 jobs, default 6 hours. Now 20 minutes (CI) / 30 (release). |
| `ci.yml` declares no permissions | **fixed** | Now `contents: read` at workflow level. |
| No JavaScript is ever executed by the test suite or CI | **fixed** | 29 frontend tests under `node --test`, wired into CI. Found and fixed three real defects while being written. |

Three defects were found *by* this pass rather than reported in the audit:

- **`node --test test/` stopped working at node 20+**, so the CI step added the
  day before would have failed on node 25 while passing locally on 18.
- **`committed` was assigned inside `_run`'s `try`** and read by its `except`
  handler, so anything raising before that point crashed the error handler with an
  `UnboundLocalError` — the one place that must not fail. Surfaced by adding a new
  early return above it; the hazard was already there.
- **The new `leak-gates` job scanned all of history, not the pull request.** The
  message scan grandfathers upstream's pre-gate commits through a recorded SHA;
  that reasoning covered upstream (has the anchor) and the published export (no
  shared commits, clean either way) but not a downstream replica, which carries
  some of upstream's old commits and *not* the anchor. So the job re-flagged five
  2026-05 messages nobody in the pull request wrote, on a repository whose ruleset
  forbids the force push that would fix it — permanently red, which is how a gate
  gets routed around. The job now derives the range from the event
  (`QH_SCAN_REV_RANGE`), and the tests pin both directions: a leak *inside* the
  range still fails the build.

  The five messages themselves are a real, separate finding, and narrowing the
  range does not clean them — see **Needs an operator** below.

---

## Verified as already closed by an earlier round

Re-checked against the code in this pass, not taken on trust:

- json/jsonb, array and composite columns bypassing both masking layers (M3)
- The default column-name catalog mangling ordinary columns (M5)
- The SQL-safety verdict unenforced by tests at both enforcement points (M4)
- Commit messages bypassing every gate (M1) — and the attribution-trailer
  false positive that came out of it is closed too
- README screenshots being design-mock renders (R2), the falsified competitive
  claim (M6/F4.1), LGPL third-party notices (`THIRD_PARTY_NOTICES.md`, enforced
  by the Dockerfile), `QueryHubWeb/` excluded from the scanner (it is scanned),
  binary assets bypassing the gates (the export has a raster brand scan)

---

## Open, with the reason

These are real and not done. The reason matters more than the count.

**Needs a design decision, not an implementation**

- No partitioning on `audit_log` / `requests`; `web_sessions` never pruned; the
  hourly full `schema_catalog` rewrite. All three are the same question — what the
  retention and growth model is — and answering it per table without deciding the
  model produces three inconsistent answers.
- Single-process executor, in-memory throttle, hardcoded pool of 4. These are one
  decision about whether the executor is allowed to be multi-process, which
  changes the claim/lease design that B6 just settled.
- No tenancy dimension anywhere in the model. Correctly listed; adding one is a
  schema-wide change, not a fix.

**Needs an operator, not a commit**

- The container image the README's primary install path pulls has never been
  published (GHCR release).
- Dependabot alerts are DISABLED on the public repository — measured 2026-08-08.
- Retention and maintenance jobs ship as documentation; nothing schedules them in
  the container. Wiring them needs a decision about whether the image runs a
  scheduler at all.
- **Five 2026-05 commit messages carry what the gate forbids** — three name a
  colleague, two hold an operator-specific absolute path. They sit before the
  grandfather anchor, so upstream exempts them by an explicit cost decision (391
  of 449 commits, nine branches, three tags to rewrite, for a repository that is
  never published). They are also inherited by the downstream replica, where they
  are *not* exempt and the org ruleset forbids the force push a rewrite needs. So
  the choice is the operator's: ask the org to allow a one-off history rewrite on
  the replica, or accept the messages there as grandfathered too and record that
  decision. Narrowing the scan range makes the gate usable again; it does not make
  these clean. Measured 2026-08-10.

**Real, small, not yet done**

Grouped because each is a contained change with a test, and the batch is the next
obvious piece of work:

- Migration runner inherits the app pool's 10s `statement_timeout`; per-file
  transactions make `CREATE INDEX CONCURRENTLY` structurally impossible
- `install.sh` never creates `/var/lib/queryhub` or `/var/log/queryhub`, and
  points the unit at TLS files it may not have created
- A control-DB blip at boot leaves the web process broken while `/healthz`
  reports 200
- Master key briefly world-readable between creation and `chmod`
- Two production connections hardcode `sslmode="require"`, silently downgrading
  an operator who configured `verify-full`
- Metadata-DB connections set no `sslmode` and expose no knob
- `routes_avatar`'s SSRF defence checks the status code after urllib has already
  followed the redirect
- Requester-controlled `database_name` is unvalidated free text and lands
  unescaped in the approval card
- CSV-import table probe DMs the raw driver exception, bypassing `errors.scrub`
- Scoped ('dba') admins can read the whole fleet's audit trail, including SQL
  text for targets outside their scope
- The web session signing secret is derived from the raw master-key FILE rather
  than the parsed key ring, so any edit invalidates every live session
- Set-operation arms 2..n invisible to the source-column resolver (adjacent to
  the EXPLAIN-lineage work; may already be covered — needs measuring, not
  assuming)

**Documentation contradictions**

A cluster, and they should be fixed together by reconciling the documents against
the code once rather than one at a time: `ARCHITECTURE.md`'s transport-agnostic
claim, `DISASTER_RECOVERY.md` vs `KEY_ROTATION.md`, `OPERATIONS.md` §4 vs the
migration runner, `SCHEMA.md`'s migration history stopping at 054, the three
different descriptions of `kill_switch`, `FEATURES.md` predating the vanilla
profile, and the stale test counts in `KNOWN_LIMITATIONS.md` and `ROADMAP.md`.

**Strategy items**

The positioning work (rewrite the competitive section around "the statement picks
the credential", demote "nothing in your data path", file six good-first-issues,
the v0.2 MySQL-first plan, and the explicit do-not-build list) is a set of
decisions for the maintainer, not code. Recorded here so they stop being counted
as engineering backlog.

---

## How to keep this honest

When an item moves, move it in this file in the same commit, and say which of the
six statuses it moved to and what was measured. An entry that says "fixed" with no
test is an entry that will be wrong within a month.
