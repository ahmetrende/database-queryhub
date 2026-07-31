# Personal data in QueryHub

What this system stores about people, how long it keeps it, how to get rid of
it, and where the honest limits of the "PII masking" claim are.

This is not legal advice and it is not a certification. QueryHub has no SOC 2
report and no ISO 27001 certificate; it is a self-hosted tool, so the controller
of the data is **you**. What this document does is tell you exactly what you are
controlling, because you cannot answer a data-subject request or a DPIA question
about software that will not say what it stores.

> **The short version.** QueryHub stores who asked for what, and the SQL they
> asked. It does not store query *results* beyond a short TTL. The identities are
> your own staff, not your customers — but the SQL text is written by humans and
> can contain anything, including a customer's email address in a WHERE clause,
> and that text is retained for as long as the audit trail is.

## 1. Who the data subjects are

Two groups, and the distinction matters for a retention argument:

| | Who | Where it comes from |
| --- | --- | --- |
| **Operators and developers** | your own staff — the people who submit, approve and administer | the identity provider (Slack profile, or a local account you create) |
| **Third parties** | anyone whose data appears *inside* a SQL statement or its results | typed by a developer, or read from your target databases |

The first group is unavoidable: an audit trail with no actor is not an audit
trail. The second group is the one to think about, and section 4 is about
limiting it.

## 2. What is stored, by table

Generated from the live schema, not from memory. "Actor" means one of your own
people; "content" means free text that may contain anything.

### Identity and access (actor data)

| Table | Personal data | Retention |
| --- | --- | --- |
| `requesters` | principal id, name, email, timezone | until removed by an operator |
| `local_users` | username, display name, email, **password hash** (PBKDF2, salted — never the password) | until removed by an operator |
| `admins`, `team_members`, `temp_admin_grants` | principal id, name, email, timezone | until removed |
| `user_target_grants`, `import_grants`, `auto_approve_grants`, `user_row_limit_overrides`, `report_excluded_users` | principal id, plus a free-text `reason` | until revoked; rows persist after revocation as a record |
| `web_sessions` | principal id, **user agent**, avatar URL | until expiry/revocation; swept by the retention job (migration 077) |

### Requests and the audit trail (actor data + content)

| Table | Personal data | Retention |
| --- | --- | --- |
| `requests` | requester principal id and name, approver id and name, **the SQL text**, decision reason | **indefinite** — this is the audit trail |
| `audit_log` | actor id and name, a `details` JSON blob | **indefinite** |
| `access_requests`, `auto_approve_requests` | requester/approver identity, reason, **the attempted SQL** | indefinite |
| `csv_imports` | requester/approver identity, target table | indefinite |
| `submission_failures` | principal id and name, **the rejected SQL** | indefinite |
| `request_ratings` | principal id, **free-text feedback** | indefinite |
| `query_favorites`, `query_templates`, `web_saved_sessions` | owner principal id, **saved SQL** | until the owner deletes it |

### Query results (the highest-risk data, deliberately short-lived)

Result files — CSV and XLSX, written under the results directory
(`QH_RESULTS_DIR`) — hold whatever the query returned, which for a query against
a customer table is customer data. They are:

- **masked on the way out** (section 4),
- **deleted after `bot_config.results_ttl_hours`** (default 72), by
  `scripts/cleanup_old_results.py`, from disk *and* from Slack, and
- **not backed up** — see `docs/DISASTER_RECOVERY.md`, which explicitly excludes
  them.

The result *file* is transient. The *statement* that produced it is not.

### Not personal data, but worth knowing

`target_servers` holds database credentials (Fernet-encrypted), and
`schema_tables` / `schema_columns` hold your target schemas — table and column
names, no rows.

## 3. Retention: what the defaults actually are

| Data | Default | How to change it |
| --- | --- | --- |
| Query result files | 72 hours | `bot_config.results_ttl_hours` |
| Web sessions | 12 hours (refresh token) | `web_refresh_token_hours`; expired rows swept by the retention job |
| Authorization-event outbox | drained continuously, then pruned | the retention job |
| **Requests, audit log, ratings, failures** | **forever** | no built-in expiry — see below |

The last row is a deliberate design decision, not an oversight: the audit trail
is the product's central promise, and a gateway that quietly forgets who ran what
is worse than no gateway. But "forever" is a choice *you* are making about
personal data, so make it knowingly:

- If your retention policy requires a limit, add a scheduled job that deletes or
  anonymises `requests` and `audit_log` rows past your horizon. There is no
  supported tooling for that yet (it is on the roadmap as retention /
  partitioning); a `DELETE ... WHERE created_at < now() - interval 'N days'` is
  the honest interim, and you should decide whether to keep the row with the
  identity nulled rather than delete it.
- If your policy allows indefinite retention of *operational* logs, note that the
  SQL text is the part most likely to contain third-party data, and consider
  clearing `requests.query` past a horizon while keeping the identity and the
  decision — that preserves the accountability record and drops the content.

## 4. Limiting third-party data: what masking does and does not do

PII masking rewrites values as they stream into the result file. Two layers:

1. **Content detectors** — by value, not by column name, so an aliased or wrapped
   column cannot slip past: IBAN (ISO 13616 mod-97), payment card (Luhn +
   network prefix), email, E.164 phone, plus the national identifiers of the
   configured region pack (`bot_config.pii_region`).
2. **Column-name catalog** (`pii_column_patterns`) for free-text PII with no
   detectable shape — a name, an address — matched on the result column name.

**Be clear about what this is.** It is *accidental-exposure mitigation*, not a
data boundary:

- It masks what leaves in the result file. It does not stop a developer from
  putting personal data in the query itself, and the query is retained.
- Layer 1 catches formats. A free-text `notes` column containing a person's
  address is only masked if a pattern in layer 2 matches the column name.
- Layer 2 matches the **output** column name. Since QueryHub resolves lineage
  through derived tables, CTEs and set operations, an alias does not defeat it —
  but a column the catalog has never heard of is not masked.
- Nothing here is a substitute for column privileges, row-level security, or
  masking views on the target database. Those are enforced by the database. This
  is enforced by QueryHub, and QueryHub is not the only way into your data.

If you need a hard guarantee that a column can never be read, revoke SELECT on
it. Use masking for the case it is good at: a developer running a legitimate
query and not needing to see the customer's card number to answer the question.

## 5. Answering a data-subject request

For one of your own staff (an operator or developer):

```sql
-- 1. What identity does this person have?
--    Slack:  U0XXXXXXXXX     Local:  local:<username>
-- 2. Everything keyed on it:
SELECT 'requests' AS t, count(*) FROM requests WHERE requester_slack_id = :p
UNION ALL SELECT 'audit_log', count(*) FROM audit_log WHERE actor_slack_id = :p
UNION ALL SELECT 'ratings',   count(*) FROM request_ratings WHERE slack_user_id = :p
UNION ALL SELECT 'favorites', count(*) FROM query_favorites WHERE slack_user_id = :p
UNION ALL SELECT 'sessions',  count(*) FROM web_sessions WHERE slack_user_id = :p;
```

**Access / portability** is that query set. **Erasure** needs a decision you
have to make explicitly:

- *Directory data* (`requesters`, `local_users`, `admins`, grants) can be deleted
  outright. Do it, and revoke their sessions: `sessions.revoke_user()`.
- *Audit rows* cannot be deleted without destroying the record of decisions that
  affected production. The usual resolution is to keep the row and pseudonymise
  the actor: replace `actor_name` with a tombstone and keep `actor_slack_id` as
  an opaque reference, or replace both with a deletion marker if your legal basis
  for retention does not survive the request. QueryHub does not decide this for
  you and does not ship a script that pretends to.

For a **third party** whose data appeared in a query or result: the result file
is already gone after the TTL; what remains is the SQL text in `requests.query`.
Search it (`WHERE query ILIKE ...`) and treat those rows under the same
keep-or-pseudonymise decision.

## 6. Data residency and transfers

QueryHub is self-hosted and makes **no outbound network calls of its own** other
than to the systems you configure:

- your metadata database,
- your target databases,
- Slack's API — only if you enable the Slack profile. That is a transfer to
  Slack (and therefore, typically, to the US): the approval card carries the
  requester's identity and the SQL text, and result files are uploaded as Slack
  files. **The vanilla profile makes no Slack calls at all**, which is the
  configuration to choose if a US transfer is a problem for you.
- AWS Secrets Manager — only if you select that secrets provider.

The web UI has no third-party assets: fonts are self-hosted, there is no CDN, no
analytics, and no telemetry. Nothing phones home; there is no "usage statistics"
switch because there is no such feature.

The optional metrics dashboard publishes to an object store **you** name, and it
contains identities and the access matrix — see the warning in
`docs/OPERATIONS.md` about access-controlling that bucket.

## 7. What is missing, plainly

- No built-in retention for `requests` / `audit_log` (section 3).
- No erasure tooling (section 5) — by choice, because the correct action is a
  policy decision, but it does mean the work is manual.
- No append-only or externally-anchored audit log yet: an operator with database
  access can edit `audit_log`. The control-plane grant block exists so QueryHub
  cannot be used to do it *through QueryHub*, but a DBA with direct access is
  outside the tool's control. Immutable audit is on the roadmap.
- No DPA template, no sub-processor list — there is no vendor here to sign one;
  you are the processor.
- No certifications. If a buyer requires SOC 2, this is not that.
