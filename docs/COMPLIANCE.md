# Personal data in QueryHub

This document says what this system stores about people and how long it keeps
it. It also says how to delete that data, and where the honest limits of the
"PII masking" claim are.

This is not legal advice, and it is not a certification. QueryHub has no SOC 2
report and no ISO 27001 certificate. It is a self-hosted tool, so **you** are
the controller of the data. This document tells you exactly what you are
controlling. You need that, because you cannot answer a data-subject request or
a DPIA question about software that will not say what it stores.

> **The short version.** QueryHub stores who asked for what, and the SQL they
> asked. It does not store query *results* beyond a short TTL. The identities are
> your own staff, not your customers. But humans write the SQL text, and it can
> contain anything, including a customer's email address in a WHERE clause.
> QueryHub keeps that text for as long as it keeps the audit trail.

## 1. Who the data subjects are

There are two groups, and the distinction matters for a retention argument:

| | Who | Where it comes from |
| --- | --- | --- |
| **Operators and developers** | your own staff — the people who submit, approve and administer | the identity provider (Slack profile, or a local account you create) |
| **Third parties** | anyone whose data appears *inside* a SQL statement or its results | a developer types it, or a query reads it from your target databases |

The first group is unavoidable: an audit trail with no actor is not an audit
trail. The second group is the one to think about. Section 4 is about how to
limit it.

## 2. What is stored, by table

The tables below come from the live schema, not from memory. "Actor" means one
of your own people. "Content" means free text that may contain anything.

### Identity and access (actor data)

| Table | Personal data | Retention |
| --- | --- | --- |
| `requesters` | principal id, name, email, timezone | until an operator deletes it |
| `local_users` | username, display name, email, **password hash** (PBKDF2, salted — never the password) | until an operator deletes it |
| `admins`, `team_members`, `temp_admin_grants` | principal id, name, email, timezone | until deleted |
| `user_target_grants`, `import_grants`, `auto_approve_grants`, `user_row_limit_overrides`, `report_excluded_users` | principal id, plus a free-text `reason` | until revoked. The rows persist after revocation as a record |
| `web_sessions` | principal id, **user agent**, avatar URL | until expiry/revocation. The retention job (migration 077) sweeps the rows |

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

Result files hold whatever the query returned. For a query against a customer
table, that is customer data. QueryHub writes them as CSV and XLSX files under
the results directory (`QH_RESULTS_DIR`). Result files are:

- **Masked on output** (section 4).
- **Deleted after `bot_config.results_ttl_hours`** (default 72), from disk *and*
  from Slack. `scripts/cleanup_old_results.py` deletes them.
- **Not in backups.** See `docs/DISASTER_RECOVERY.md`, which explicitly excludes
  them.

The result *file* is transient. The *statement* that produced it is not.

### Not personal data, but worth knowing

`target_servers` holds database credentials (Fernet-encrypted).
`schema_tables` / `schema_columns` hold your target schemas: table and column
names, no rows.

## 3. Retention: what the defaults actually are

| Data | Default | How to change it |
| --- | --- | --- |
| Query result files | 72 hours | `bot_config.results_ttl_hours` |
| Web sessions | 12 hours (refresh token) | `web_refresh_token_hours`. The retention job sweeps expired rows |
| Authorization-event outbox | drained continuously, then pruned | the retention job |
| **Requests, audit log, ratings, failures** | **forever** | no built-in expiry — see below |

The last row is a deliberate design decision, not an oversight. The audit trail
is the product's central promise. A gateway that quietly forgets who ran what is
worse than no gateway. But "forever" is a choice *you* make about personal data,
so make it knowingly:

- If your retention policy requires a limit, add a scheduled job for it. The job
  deletes or anonymises `requests` and `audit_log` rows past your horizon. There
  is no supported tooling for that yet. It is on the roadmap as retention /
  partitioning. The honest interim is a
  `DELETE ... WHERE created_at < now() - interval 'N days'`. You should decide
  whether to keep the row with the identity nulled, rather than delete it.
- If your policy allows indefinite retention of *operational* logs, consider
  clearing `requests.query` past a horizon. The SQL text is the part most likely
  to contain third-party data. Keep the identity and the decision when you clear
  it. That preserves the accountability record and drops the content.

## 4. Limiting third-party data: what masking does and does not do

PII masking rewrites values as they stream into the result file. It has two
layers:

1. **Content detectors** match by value, not by column name, so an aliased or
   wrapped column cannot evade them. They catch IBAN (ISO 13616 mod-97), payment
   card (Luhn + network prefix), email, E.164 phone, plus the national
   identifiers of the configured region pack (`bot_config.pii_region`).
2. **Column-name catalog** (`pii_column_patterns`) for free-text PII with no
   detectable shape, such as a name or an address. It matches on the result
   column name.

**Be clear about what this is.** It is *accidental-exposure mitigation*, not a
data boundary:

- It masks what leaves QueryHub in the result file. It does not stop a developer
  from putting personal data in the query itself, and QueryHub retains the query.
- Layer 1 catches formats. QueryHub masks a free-text `notes` column with a
  person's address only if a pattern in layer 2 matches the column name.
- Layer 2 matches the **output** column name. QueryHub resolves lineage through
  derived tables, CTEs and set operations, so an alias does not defeat it. But a
  column that the catalog does not know stays unmasked.
- Nothing here replaces column privileges, row-level security, or masking views
  on the target database. The database enforces those. QueryHub enforces this,
  and QueryHub is not the only way into your data.

If you need a hard guarantee that nobody can ever read a column, revoke SELECT
on it. Use masking for the case it is good at. In that case, a developer runs a
legitimate query and does not need to see the customer's card number to answer
the question.

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

**Access / portability** is that query set. **Erasure** needs an explicit
decision from you:

- *Directory data* (`requesters`, `local_users`, `admins`, grants): you can
  delete it outright. Delete it. Then revoke their sessions with
  `sessions.revoke_user()`.
- *Audit rows*: you cannot delete them without destroying the record of
  decisions that affected production. The usual resolution is to keep the row
  and pseudonymise the actor:
  - Replace `actor_name` with a tombstone, and keep `actor_slack_id` as an
    opaque reference.
  - Or replace both with a deletion marker, if your legal basis for retention
    does not survive the request.

  QueryHub does not decide this for you. It does not ship a script that pretends
  to.

For a **third party** whose data appeared in a query or result, the result file
is already gone after the TTL. What remains is the SQL text in `requests.query`.
Search it (`WHERE query ILIKE ...`). Treat those rows under the same
keep-or-pseudonymise decision.

## 6. Data residency and transfers

QueryHub is self-hosted. It makes **no outbound network calls of its own**,
except to the systems you configure:

- your metadata database,
- your target databases,
- Slack's API, only if you enable the Slack profile. That is a transfer to Slack
  (and therefore, typically, to the US). The approval card carries the
  requester's identity and the SQL text, and QueryHub uploads result files as
  Slack files. **The vanilla profile makes no Slack calls at all.** Choose that
  configuration if a US transfer is a problem for you.
- AWS Secrets Manager, only if you select that secrets provider.

The web UI has no third-party assets. Fonts are self-hosted, and there is no
CDN, no analytics, and no telemetry. Nothing sends data to the authors of
QueryHub. There is no "usage statistics" switch, because there is no such
feature.

The optional metrics dashboard publishes to an object store that **you** name.
It contains identities and the access matrix. See the warning in
`docs/OPERATIONS.md` about access control for that bucket.

## 7. What is missing, plainly

- No built-in retention for `requests` / `audit_log` (section 3).
- No erasure tooling (section 5). This is by choice, because the correct action
  is a policy decision. But it does mean the work is manual.
- No append-only or externally-anchored audit log yet. An operator with database
  access can edit `audit_log`. The control-plane grant block exists so that
  nobody can do it *through QueryHub*. But a DBA with direct access is outside
  the tool's control. Immutable audit is on the roadmap.
- No DPA template, no sub-processor list. There is no vendor here to sign one.
  You are the processor.
- No certifications. If a buyer requires SOC 2, this is not that.
