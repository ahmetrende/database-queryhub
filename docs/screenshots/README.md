# Screenshots

Every image here is the shipped UI rendered from **mock data**. No real
connection alias, database, person or query appears in any of them, and none was
captured against a running deployment.

## How they are produced

The web images come from the prototype — `QueryHubWeb/QueryHub.html` opened over
a local static server with `?brand=warm`, which runs entirely from
`qh-api-mock.jsx` with no backend. That is the point: the prototype cannot reach
a real database, so a screenshot taken from it cannot leak one.

The Slack images come from Slack's own [Block Kit
Builder](https://app.slack.com/block-kit-builder), fed the exact payloads this
code sends — `modal.build_modal()` and `notifications._request_blocks()` called
with mock values. Slack renders its own chrome, so these are neither a drawing of
Slack nor a capture of a workspace.

## What each one shows

| File | Surface | What it is for |
|---|---|---|
| `hero.png` | web, admin | The headline: a pending write with its SQL, classification, estimated impact, reason, and Approve / Reject / Request changes. The product's one sentence in one screen. |
| `flow.gif` | web | The loop end to end (11 s): read-only query auto-approved within a grant and returned masked → the DBA side → the approval → the audit entry. |
| `web/welcome.png` | web, developer | The workspace — connections by tier, recent history, saved queries, sessions. |
| `web/editor.png` | web, developer | SQL against a tier-matched connection, a masked PII column in the result, timing and row count. |
| `web/editor-light.png` | web, developer | Light theme, SQL Server schema — the second engine. |
| `web/grants.png` | web, admin | The three-tier model as an operator sees it: subject, connection, database scope, tier. |
| `web/audit.png` | web, admin | Every decision attributed — approvals, rejections, auto-approvals, grant changes, and a query the safety pass refused. |
| `web/autoapprove.png` | web, admin | Time-limited, row-capped review exemptions. |
| `web/login.png` | web | Sign-in: Slack, or a local account when there is no Slack. |
| `slack/slack-modal.png` | Slack | `/sql` — the submit modal. |
| `slack/slack-approval.png` | Slack | The card a DBA acts on, with the statement inlined. |

## If you re-shoot or add an image

**Keep it honest about the engines.** Warm theme, dark (except
`editor-light.png`), and check before committing that the connection sidebar
shows only engines the product can actually execute — `prod-main`,
`prod-replica`, `staging`, `reporting-mssql`. An earlier set advertised Oracle,
ClickHouse, Couchbase and MySQL with vendor logos and PROD badges, none of which
QueryHub can run a query against.

**The README must reference it.** A test fails on any file in this directory that
no page shows: an unreferenced image is either a leftover or an oversight, and a
caption pointing at a missing file renders as a broken image on the landing page.

**Prefer the sample personas already in the mock** — Dana Kaur, Marco Reyes,
Priya Nair, Ali Osman — so the names on screen match the names in
`qh-api-mock.jsx`.
