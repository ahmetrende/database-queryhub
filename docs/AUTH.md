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
  assertion minted for one call against another call.
- It refuses a reused `jti` through the `idp_assertion_jti` ledger. The
  ledger keeps each `jti` until its token's expiry plus the skew.
- It applies the `web_allowed_email_domain` gate. Unlike the OIDC
  providers, it refuses every assertion while that setting is empty.
- It resolves the asserted address to an existing `requesters` or `admins`
  row and proceeds as that principal. It refuses an unknown address and
  never creates a row for it.

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
  `POST /api/admin/notifications/outbox/{id}/processed` — the pending-request
  feed and its acknowledgement. QueryHub writes rows only while
  `idp_outbox_enabled = on`. The daily cleanup deletes them after
  `idp_outbox_retention_days`.

Create the sync account as a **disabled** `requesters` row with no
grants. Give it the portal's sync address as its email. Then:

- The assertion still resolves it (the resolver matches disabled rows).
- The machine gate accepts it.
- Every other route refuses it.

It needs no admin row. Under the access model, an admin role is
fleet-wide. So an admin row would give the portal's cron key approval
authority that it has no use for.

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
  → hand off to the endpoint, which does its own per-query grant checks.
```

This runs on **every** request, cheaply: a signature check and one indexed
DB lookup, with no identity-provider call. That alone closes most holes,
because the token is short-lived. A deactivated user's session dies within
the token window, even if nothing else fires.

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
