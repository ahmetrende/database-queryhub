# QueryHub Web — Authentication & Session Security

This document answers a recurring question: **"the backend needs to keep
checking that the person is still legitimate — how, without a security
hole?"**

Short version: **login is one-time, authorization is continuous, and
continuous ≠ calling an identity provider on every request.** Continuous
authorization has three parts:

- a short-lived session token
- re-verification at refresh
- a live check at the moment of the dangerous action (running RW/DDL
  against production)

All of it uses one dependency (`web/deps.py::current_user`), so no
endpoint can stay unprotected by accident.

The code cross-references the section numbers below (`AUTH.md §3`, `§4`,
`§5`). Keep them stable when you edit this file.

---

## 1. Login providers

The canonical identity everywhere in QueryHub is one **principal id**.
Grants, admins, teams and audit all key on it. A login provider's only job
is to produce that id safely (`web/auth_providers.py`):

- **Slack SSO** (OpenID Connect): returns the Slack user id directly
  (zero email-to-user mapping). It pins access to *your* workspace through
  `team_id`. Principal id: the Slack member id (`U…`). Toggle:
  `web_auth_slack_enabled`.
- **Local accounts**: built-in username/password for the vanilla
  (no-Slack) profile. QueryHub stores passwords only as salted PBKDF2
  hashes (`passwords.py`), never as cleartext. Principal id:
  `local:<username>`. Toggle: `web_auth_local_enabled`.
- **External OIDC providers — any number of them.** A deployment with a
  company identity provider (authentik, Keycloak, Okta, Auth0, Google…)
  configures it in the environment. The deployment then gets a sign-in
  button. A second provider is three more variables. These providers do
  **not** mint a principal. QueryHub searches `requesters` / `admins` for
  the provider's *verified* email, and the login proceeds as the principal
  already on that row. Toggle: `web_auth_<id>_enabled`.

Slack and local accounts are distinct principals (no cross-provider identity
merge). An OIDC provider is the opposite by design. It is a new way to prove
an existing identity. So a person's grants, history and audit trail are the
same, whichever button they used.

### 1.1 Configuring an OIDC provider

Secrets live in the environment (`/etc/queryhub/web.env`), never in
`bot_config`. Every instance on the same bot DB shares that table, and
people can read it through the admin config screen.

```
OIDC_CORP_ISSUER=https://sso.example.com/application/o/queryhub/
OIDC_CORP_CLIENT_ID=…
OIDC_CORP_CLIENT_SECRET=…
OIDC_CORP_SCOPES=openid email profile     # optional
OIDC_CORP_LABEL=Sign in with Corp SSO     # optional
```

`CORP` becomes the provider id `corp`. The id fixes the URL that you
register with the identity provider:

```
https://<queryhub-base-url>/api/auth/corp/callback
```

The id is one lowercase alphanumeric token. It must not be `slack` or
`local`. You should treat it as permanent once you register it. QueryHub
reads the endpoints from the issuer's `/.well-known/openid-configuration`
(cached an hour), so a rotation on the provider's side needs no change
here.

**What the flow enforces**, beyond a plain authorization-code exchange:

| Check | Why |
| --- | --- |
| PKCE (S256) + `nonce` | QueryHub derives both from the signed `state` with HMAC. So the flow needs no server-side attempt table, and neither value is guessable. They bind the callback to the attempt that started it. |
| `id_token` signature, `iss`, `aud` | Standard OIDC verification, with keys from the published JWKS. |
| Algorithm allow-list | RSA/EC only. The flow refuses `none` and the HMAC family: with `HS256`, the client secret is also the verification key. |
| `email_verified` | The email is the join to someone's grants. An unverified address would let a user type a colleague's address. |
| `web_allowed_email_domain` | The same domain gate that the Slack provider uses. |
| Unambiguous lookup | Two rows that share an address resolve to **nothing**. If the flow picked either row, one person would get another person's grants. |
| No auto-onboarding | The flow refuses an address with no row and never creates one. Otherwise everyone the company IdP knows would become a QueryHub user. |

A person who signs in this way still needs a Slack id on their row, for
two reasons:

- Approvals and result delivery are Slack DMs.
- `users.info` stays the "still an employee?" oracle at every refresh
  (§4).

---

### 1.2 Identity assertions from a trusted portal (service to service)

This is a third way in, for a portal that proxies QueryHub for its own
signed-in users. There is no browser session. Every request carries an
`X-IDP-Assertion` header with a 60-second JWT, which the portal signs with
Ed25519 (`EdDSA`).

`verify()` in `web/idp_assertion.py` checks the token:

- It finds the key by `kid` in `bot_config.idp_public_keys` (a JSON
  object, kid → PEM).
- It requires `exp`, `iat`, `sub`, `jti`, `aud` and `iss`. It checks `aud`
  / `iss` against `idp_audience` / `idp_issuer` (defaults `queryhub` /
  `idp`).
- It tolerates `idp_clock_skew_seconds` (default 10, at most 60) of clock
  disagreement on `iat` and `exp`. It refuses an assertion whose
  `exp - iat` is over 120 seconds.

It also checks the request and the address:

- It checks that the assertion is bound to THIS request: the method, the
  path with its query string, and the body. So nobody can replay an
  assertion minted for one call against another call. The signed path is
  the decoded path (Go `u.Path`). The query string stays raw (Go
  `u.RawQuery`).
- It refuses a reused `jti` through the `idp_assertion_jti` ledger. The
  ledger keeps each `jti` until its token's expiry plus the skew.
- It applies the `web_allowed_email_domain` gate. Unlike the OIDC
  providers, it refuses every assertion while that setting is empty
  (`no_domain`).
- It resolves the asserted address to an existing `requesters` or `admins`
  row and proceeds as that principal. It refuses an unknown address and
  never creates a row for it. It also refuses an address that rows of two
  different people share (`ambiguous_address`).

**Refusals.** QueryHub answers a refused assertion with `401`. Three
refusals concern the person, not the portal. For these, `error.code` names
the cause, and `error.message` tells the person what to do:

| `error.code` | Cause | `error.message` |
| --- | --- | --- |
| `not_onboarded` | No `requesters` or `admins` row has the address. | Refused: QueryHub has no account for this address. Ask an admin to add you. |
| `bad_domain` | The address is outside `web_allowed_email_domain`. | Refused: this address is outside the allowed domain. |
| `ambiguous_address` | Rows of two or more different people have the address. | Refused: two QueryHub accounts share this address. Ask an admin to fix it. |

QueryHub sends these codes only after the signature, the request binding
and the `jti` pass. So a forged token cannot find out whether an address
has an account. Every other refusal is a seam failure, a fault of the
portal, the keys or the configuration: `expired`, `clock_ahead`,
`too_long_lived`, `bad_signature`, `bad_audience`, `bad_issuer`,
`unknown_kid`, `body_mismatch`, `replayed`, `malformed`, `disabled` and
`no_domain`. For each of these, `error.code` is `unauthenticated` and
`error.message` is "Refused: the identity assertion is not valid." Only the
log line `idp assertion refused: <code>` names the cause.

**Shared addresses.** `GET /api/admin/people/duplicate-addresses` lists
every address that gives `ambiguous_address`, with the rows that share it.
Only a super-admin can read it (the `access` gate), and it changes nothing.
It also answers a browser session, so it works while the assertion path is
off. `GET /api/admin/people` carries no addresses, so the portal cannot
find the duplicates without this list.

```json
{"duplicates": [
   {"email": "shared@example.com",
    "accounts": [
      {"table": "requesters", "principal": "U0EXAMPLE01",
       "name": "Example Requester", "enabled": true},
      {"table": "admins", "principal": "U0EXAMPLE02",
       "name": "", "enabled": false}]}],
 "total": 1}
```

- `email` is the address in lowercase, without outer spaces. `table` is
  `requesters` or `admins`. `name` is an empty string when the row has no
  name. `total` is the number of addresses.
- One person with a row in each table is not a duplicate. A row with no
  address is never a duplicate.
- An OIDC login refuses the same addresses, as `not_onboarded`.
- The list is usually empty. While `access_model_mirror` is on, the
  database refuses a second principal with the same address. It does not
  refuse two addresses that differ only in outer spaces. It also does not
  refuse an admin row whose address differs from the requester row of the
  same person.

For a request that carries the header, the assertion is the ONLY judge.
After `current_user` refuses an assertion, it never uses the cookie
instead. Such requests record `origin = idp`, next to `slack` and `web`.
Before verification, QueryHub refuses a websocket handshake that carries
the header, so the handshake cannot spend a `jti`. The portal polls for
live status instead.

Everything here is inert until `bot_config.idp_assertion_enabled = on`.
Beyond the proxied `/api` surface, the portal gets two machine-only things.
Both are behind `require_sync_principal`: the caller must be the principal
named in `bot_config.idp_sync_principal`, and the caller must arrive
through an assertion. Admin rights are neither needed nor enough.

- `POST /api/admin/principals/sync` — the reconcile. It enables and
  disables `requesters` rows to match the list of addresses that the
  portal sends. It never writes `admins`, and it refuses a list that would
  disable every requester. It returns the addresses that it cannot
  resolve, for a human to onboard. It writes an `idp_principal_sync` audit
  row.

  **The sync disables every requester who is missing from the list.** The
  exceptions are live admins, holders of any access-model role and the
  sync principal itself. The response lists them in `kept`, because a
  disable of their row would also disable the roles that it carries. Send
  `"dry_run": true` first. A dry run returns what would change and writes
  nothing.
  - **QueryHub holds a run that would disable more than
    `idp_sync_max_disable` requesters (default 5).** The run changes
    nothing, enables included, and answers `409 approval_required`. Every
    super-admin gets one Slack card ("Do you approve?") that names the
    people. A wrong list from the panel is far likelier than that many
    people leaving in the same quarter-hour.
  - **Approve** covers exactly those people, once, for 24 hours. The next
    run whose disable list lies inside the approved set applies.
    **Reject** keeps the same list blocked for as long as it keeps
    arriving. A different list is a new question. QueryHub asks again
    about an undecided list after a day.
  - The response carries a `guard` object (`state`, `limit`,
    `would_disable`). A dry run adds `would_hold` for a list that would be
    held. Without Slack there is no card. Raise `idp_sync_max_disable` for
    one run instead.
- `GET /api/admin/notifications/outbox` and
  `POST /api/admin/notifications/outbox/{id}/processed` — the notification
  feed and its acknowledgement. QueryHub writes rows only while
  `idp_outbox_enabled = on`. The daily cleanup deletes them after
  `idp_outbox_retention_days`. §1.3 lists the events.

Create the sync account as a **disabled** `requesters` row with no
grants. Give it the portal's sync address as its email. Then:

- The assertion still resolves it (the resolver matches disabled rows).
- The machine gate accepts it.
- Every other route refuses it.

It needs no admin row. Under the access model, an admin role is
fleet-wide. So an admin row would give the portal's cron key approval
authority that it has no use for.

### 1.3 Outbox events for the portal

QueryHub writes a row next to the Slack messages, and the Slack messages
do not change. It also writes the row when Slack is off. Each event has
these fields:

- `recipients`: work emails. QueryHub stores Slack ids and finds each email
  when the portal reads the feed.
- `payload`: strings only, because the portal decodes it as a map of
  strings. An empty value is `""`, never null.
- `requestId` at the top level: the id of the SQL request, for
  `queryhub.request_pending` only. It is null for the other events. Their
  payload `requestId` is the id of the access request or the window request.

| `eventType` | QueryHub writes it when | Recipients | Payload keys |
| --- | --- | --- | --- |
| `queryhub.request_pending` | A SQL request waits for approval. | The approvers that the Slack fan-out DMs. | `requestId`, `requesterName`, `target`, `tier`, `justification` |
| `queryhub.access_request_pending` | A person asks for access to a database. | The admins that the Slack fan-out DMs. | `requestId`, `requesterName`, `target`, `database`, `tier`, `reason` |
| `queryhub.access_request_decided` | An admin approves or rejects an access request. | The requester. | `requestId`, `decision`, `target`, `database`, `tier`, `deciderName`, `note`, `grantApplied` |
| `queryhub.window_request_pending` | A person asks for an auto-approve window. | The admins that the Slack fan-out DMs. | `requestId`, `requesterName`, `target`, `database`, `tier`, `reason`, `windowLabel` |
| `queryhub.window_request_decided` | An admin approves or rejects a window request. | The requester. | `requestId`, `decision`, `target`, `database`, `tier`, `deciderName`, `note`, `windowLabel` |
| `queryhub.request_decided` | An admin approves, rejects or asks for changes on a SQL request. | The requester. | `requestId`, `decision`, `target`, `tier`, `decidedBy`, `note` |
| `queryhub.request_withdrawn` | The requester withdraws a SQL request. | The requester. | `requestId`, `target`, `tier`, `decidedBy`, `note` |
| `queryhub.request_expired` | The stale-pending job expires a SQL request. | The requester. | `requestId`, `target`, `tier`, `decidedBy`, `note` |

The three SQL request events above fill the top-level `requestId` with the
id of the SQL request. For `queryhub.request_decided`, `decision` is
`approved`, `rejected` or `changes_requested`. `note` is the admin's reason,
or `""`. No SQL request event holds the SQL text or a result value.

The values:

- `tier` is `ro`, `rw` or `ddl`, in lower case.
- `decision` is `approved` or `rejected`.
- `target` is the connection alias. For a server that QueryHub does not have
  yet, it is the name that the requester typed.
- `database` is `""` when the request names no database. On an access
  request, that means every database.
- `grantApplied` is `true` only when the approval wrote the grant. An
  approval can skip the grant, for example on a tier conflict. Then the
  requester has no access yet.
- `note` is the reason for the decision. A window decision has no reason,
  so its `note` is always `""`.
- `windowLabel` is the length of the window, for example `8h` or `7 days`.

The portal must have a template for each event type before you set
`idp_outbox_enabled = on`. The feed lists the oldest unacknowledged rows
first. A row that the portal does not acknowledge stays in front of the
newer rows.

## 2. Login flow (one-time identity)

**Slack OIDC** is a standard authorization-code round-trip:

```
Browser                 Backend                          Slack
  │   click "Sign in"     │                                │
  │──────────────────────▶│  GET /api/auth/slack/start     │
  │                       │  build authorize URL           │
  │                       │  (openid email profile;        │
  │                       │   signed state + state cookie) │
  │◀───────── 302 ────────│                                │
  │───────────────────────────────────────────────────────▶│  user approves
  │◀───────────────────── 302 with ?code&state ────────────│
  │──────────────────────▶│  GET /api/auth/slack/callback  │
  │                       │  echo state cookie (CSRF gate) │
  │                       │  exchange code ───────────────▶│  openid.connect.token
  │                       │  verify id_token sig (JWKS)    │
  │                       │  CHECK team_id == our workspace│  ← workspace gate
  │                       │  whitelist gate, mint session  │
  │◀── redirect to app ───│  (httpOnly cookies)            │
```

The workspace gate compares against the bot's **own** workspace. QueryHub
finds that workspace once through `auth.test`, so there is no extra config
key. You can also require an email domain through
`web_allowed_email_domain`.

**Local login**: `POST /api/auth/local/login` with username/password. The
server checks the password against the stored hash and mints the exact
same session. The check is constant-time, with a dummy hash for unknown
users, so timing does not leak account existence. One opaque
`bad_credentials` error covers both a wrong password and an unknown user.

**Both providers** then pass the same entry gate that `/sql` applies: an
enabled `requesters` row or an admin row.

**Mint a session:**

- a short access JWT (`sub` = principal id, `sid`, `provider`). TTL:
  `web_access_token_minutes`, default 20.
- an opaque rotating refresh token, hashed at rest in `web_sessions`. TTL:
  `web_refresh_token_hours`, default 12.

Both travel in **httpOnly + SameSite=Lax cookies** (`Secure` through
`web_cookie_secure`), never in `localStorage`, so JS cannot exfiltrate
them.

---

## 3. The verify-session dependency (continuous authorization)

Every protected endpoint uses ONE function (`deps.current_user`). The
order matters:

```
current_user(request):
  1. Extract the access token (cookie, or Bearer header). Missing → 401.
  2. Verify signature + exp. Expired → 401 (client silently refreshes, §4).
  3. Check the session row is alive (web_sessions: not revoked, not
     expired) — the per-request revocation lookup.
  4. With `web_own_ui_super_admin_only` on, refuse a person who is not a
     super-admin with 403 `ui_super_admin_only`. A portal request never
     reaches this step. Login and refresh apply the same rule.
  → hand off to the endpoint, which does its own per-query grant checks.
```

This runs on **every** request, cheaply: a signature check and one indexed
DB lookup, with no identity-provider call. That alone closes most holes,
because the token is short-lived. A deactivated user's session dies within
the token window, even if nothing else fires.

### 3.1 Admin levels

Every `/api/admin/*` route calls `require_admin(claims, level)` before it
does anything else. There are three levels:

| Level | Who passes |
|---|---|
| `review` | Any admin: a live `admin` role row. A temporary row and a row with a tier ceiling also pass. |
| `dba` | A PERMANENT (no end date) and UNSCOPED (all teams, all targets) `admin` role row. A tier ceiling is allowed. A super-admin passes too. A temporary admin and a scoped admin do not pass. An approver-only role does not pass. |
| `access` | A super-admin only: a permanent and unscoped `admin` role row with no tier ceiling. |

With `access_model_v2` off, the `dba` rule reads the `admins` table: an
enabled row with `scope_team_ids` and `scope_target_ids` both NULL. A
`temp_admin_grants` row never counts.

Four routes take the `dba` level (PLA-1592):

- `POST /api/admin/kill` sets the kill switch to on or off. Each change
  writes a `kill_switch_set` audit row. The Slack command `/sql kill` sets
  the same switch and takes the same level.
- `GET /api/admin/people/{slack_id}/effective-access` shows the access of
  one person. Each read writes an `effective_access_viewed` audit row.
- `GET /api/admin/connections` lists the connections. A super-admin gets
  the full rows. A DBA who is not a super-admin gets only the keys in
  `DBA_CONNECTION_FIELDS` (`web/routes_admin.py`). The list has no
  `credentials`, no `secretsProvider` and no `notes`. It has `health` and
  `inRotation` (OPERATIONS.md §29). In `health.error`, QueryHub replaces
  each user name that the row stores with `[redacted]`.
- `POST /api/admin/connections/{conn}/test` tests the stored read-only
  credential. For a DBA who is not a super-admin, QueryHub scrubs the error
  again and replaces the stored user name and the password with
  `[redacted]`.

All other routes keep their level. Only a super-admin can add, change,
disable or delete a connection, test a credential that is not saved, or
change the configuration. `tests/test_dba_level.py` fails when a fifth route
takes the `dba` level, and when one door of the kill switch changes its
level and the other does not.

A refused caller gets 403 with the code `forbidden`. A caller who is not an
admin reads "Refused: you need admin access." An admin who is not a DBA
reads "Refused: you need DBA access."

In `GET /api/me`, `admin.role` is `dba` for every admin who is not a
super-admin, a temporary admin too. That value does not tell if the caller
passes the `dba` level. `admin.isDba` does: it is true for a super-admin and
for a permanent, unscoped admin, and false for any other admin. It follows
the same rule as the gate.

---

## 4. Refresh = the re-verification checkpoint

When the short access token expires, the client presents its refresh
token. **That is where QueryHub re-checks the human**: at most every
15–30 minutes, not on every request:

```
POST /api/auth/refresh:
  1. Validate + rotate the refresh token (reuse of a superseded token =
     suspected theft → the whole session is revoked, force re-login).
  2. Every provider but `local`: live users.info — deleted/gone → revoke,
     401. No answer from Slack → passes only if Slack called the person
     active within `web_employment_grace_hours` (default 2); otherwise
     revoke, 401. (Local accounts have no external employment system; their
     liveness is the enabled requesters/admins row, re-checked next.)
  3. Re-check the whitelist (enabled requester or admin) — access removed
     → revoke, 401.
  4. Mint a fresh short access token.
```

Result: someone who is no longer in Slack (or is disabled in `requesters`)
loses web access within one refresh cycle, automatically.

---

## 5. Live check at the dangerous moment

The truly sensitive action is **executing against production**, not
loading a page. So, in addition to the refresh check, a **live check runs
right before an RW/DDL submit** (`routes_queries`):

```
POST /api/queries (and /queries/batch):
  ... grant checks ...
  if required tier is RW or DDL and provider != "local":
      users.info(principal) — deleted/gone → revoke every session, 401
                            — no answer    → 503, sessions kept
  ... proceed to classify / submit ...
```

The check does not run on every read: that would be slow and rate-limited.
It runs where the possible damage is real.

A write needs a live "active". An earlier answer does not count, and a
Slack outage refuses writes until it ends. The check used to fail open on
transport errors. That let an offboarded person keep writing for as long as
Slack was unreachable. Sign-in and refresh take a recent answer instead
(§4).

QueryHub records every "active" answer in `slack_liveness`. `gone` means
`deleted`, or users.info's `user_not_found` / `user_not_visible`. Local
logins skip the Slack lookup. Their gate is the whitelist row itself.

---

## 6. Instant kill switch (revocation)

For incidents ("revoke X right now"), revoke the session row:

```sql
UPDATE web_sessions SET revoked_at = NOW(), revoked_reason = '…'
WHERE slack_user_id = '<principal>' AND revoked_at IS NULL;
```

`current_user` checks liveness on every request (§3 step 3), so this cuts
access instantly, with no wait for token expiry. To block re-login as
well, disable the `requesters` row (or the `local_users` row).

---

## 7. Putting the timers together

| Mechanism | Frequency | Catches |
|---|---|---|
| Access-JWT signature + exp | every request (cheap) | expired/forged tokens. The short TTL closes most of the window |
| Session-row liveness | every request (cheap) | manual revocation, sign-out everywhere |
| Refresh: rotate + re-verify | every 15–30 min | user no longer in Slack or on the whitelist, refresh-token theft (reuse detection) |
| Live check before RW/DDL | per dangerous action | someone who loses access *between* refreshes and tries to write to prod |

**Never trust the frontend.** The server makes every real decision, in
`current_user` and the per-query grant check.

---

## 8. Frontend touch-points

- On load, the app calls `GET /api/me`. `401` → render the login screen.
- The login screen reads `GET /api/auth/providers`. It renders a Slack
  button (`/api/auth/slack/start`, full redirect) and/or the local
  username/password form, for the providers that are enabled.
- On `401` from any call mid-session, the client tries one silent
  `POST /api/auth/refresh`. If that fails, it shows the login screen.
  (Login endpoints are exempt: a 401 there means bad credentials, not an
  expired session.)
- Sign out → `POST /api/auth/signout`. It revokes the server-side session,
  not only the cookies.
