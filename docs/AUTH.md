# QueryHub Web — Authentication & Session Security

This answers the recurring question: **"the backend needs to keep checking
that the person is still legitimate — how, without a security hole?"**

Short version: **login is one-time, authorization is continuous, and
continuous ≠ calling an identity provider on every request.** It's: a
short-lived session token + re-verification at refresh + a live check at
the moment of the dangerous action (running RW/DDL against production).
All of it funnels through one dependency (`web/deps.py::current_user`) so
no endpoint can be left unprotected by accident.

The code cross-references the section numbers below (`AUTH.md §3`, `§4`,
`§5`) — keep them stable when editing.

---

## 1. Login providers

The canonical identity everywhere in QueryHub is one **principal id** —
grants, admins, teams and audit all key on it. A login provider's only job
is to produce that id safely (`web/auth_providers.py`):

- **Slack SSO** (OpenID Connect) — hands back the Slack user id directly
  (zero email-to-user mapping) and pins access to *your* workspace via
  `team_id`. Principal id: the Slack member id (`U…`). Toggle:
  `web_auth_slack_enabled`.
- **Local accounts** — built-in username/password for the vanilla
  (no-Slack) profile. Passwords are stored only as salted PBKDF2 hashes
  (`passwords.py`), never cleartext. Principal id: `local:<username>`.
  Toggle: `web_auth_local_enabled`.
- **External OIDC providers — any number of them.** A deployment with a
  company identity provider (authentik, Keycloak, Okta, Auth0, Google…)
  configures it in the environment and gets a sign-in button; configuring
  a second one is three more variables. These do **not** mint a principal:
  the provider's *verified* email is looked up in `requesters` / `admins`
  and the login proceeds as the principal already on that row. Toggle:
  `web_auth_<id>_enabled`.

Slack and local accounts are distinct principals (no cross-provider identity
merge). An OIDC provider is the opposite by design — it is a new way to prove
an existing identity, so a person's grants, history and audit trail are the
same whichever button they used.

### 1.1 Configuring an OIDC provider

Secrets live in the environment (`/etc/slackbot/web.env`), never in
`bot_config` — that table is shared by every instance on the same bot DB and
is readable through the admin config screen.

```
OIDC_CORP_ISSUER=https://sso.example.com/application/o/queryhub/
OIDC_CORP_CLIENT_ID=…
OIDC_CORP_CLIENT_SECRET=…
OIDC_CORP_SCOPES=openid email profile     # optional
OIDC_CORP_LABEL=Sign in with Corp SSO     # optional
```

`CORP` becomes the provider id `corp`, which fixes the URL you register with
the identity provider:

```
https://<queryhub-base-url>/api/auth/corp/callback
```

The id is one lowercase alphanumeric token, must not be `slack` or `local`,
and should be treated as permanent once registered. Endpoints are read from
the issuer's `/.well-known/openid-configuration` (cached an hour), so a
rotation on the provider's side needs no change here.

**What the flow enforces**, beyond a plain authorization-code exchange:

| Check | Why |
| --- | --- |
| PKCE (S256) + `nonce` | Both derived from the signed `state` via HMAC, so no server-side attempt table is needed and neither is guessable. Binds the callback to the attempt that started it. |
| `id_token` signature, `iss`, `aud` | Standard OIDC verification, keys from the published JWKS. |
| Algorithm allow-list | RSA/EC only. `none` and the HMAC family are refused — with `HS256` the client secret doubles as the verification key. |
| `email_verified` | The email is the join to someone's grants. An unverified address would let a user type a colleague's. |
| `web_allowed_email_domain` | Same domain gate the Slack provider uses. |
| Unambiguous lookup | Two rows sharing an address resolve to **nothing**. Picking either would hand one person another person's grants. |
| No auto-onboarding | An address with no row is refused, never created — otherwise everyone the company IdP knows becomes a QueryHub user. |

A person who signs in this way still needs a Slack id on their row, because
approvals and result delivery are Slack DMs, and `users.info` remains the
"still employed?" oracle at every refresh (§4).

---

## 2. Login flow (one-time identity)

**Slack OIDC** — a standard authorization-code round-trip:

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

The workspace gate compares against the bot's **own** workspace
(discovered once via `auth.test` — no extra config key). Optionally also
require an email domain via `web_allowed_email_domain`.

**Local login** — `POST /api/auth/local/login` with username/password;
the server verifies against the stored hash (constant-time, with a dummy
hash for unknown users so timing doesn't leak account existence) and mints
the exact same session. One opaque `bad_credentials` error covers both
wrong password and unknown user.

**Both providers** then pass the same entry gate `/sql` applies: an
enabled `requesters` row or an admin row. **Mint a session:** a short
access JWT (`sub` = principal id, `sid`, `provider`; TTL
`web_access_token_minutes`, default 20) plus an opaque rotating refresh
token (hashed at rest in `web_sessions`; TTL `web_refresh_token_hours`,
default 12). Both ride **httpOnly + SameSite=Lax cookies** (`Secure` via
`web_cookie_secure`) — never `localStorage`, so JS can't exfiltrate them.

---

## 3. The verify-session dependency (continuous authorization)

Every protected endpoint goes through ONE function
(`deps.current_user`). Order matters:

```
current_user(request):
  1. Extract the access token (cookie, or Bearer header). Missing → 401.
  2. Verify signature + exp. Expired → 401 (client silently refreshes, §4).
  3. Check the session row is alive (web_sessions: not revoked, not
     expired) — the per-request revocation lookup.
  → hand off to the endpoint, which does its own per-query grant checks.
```

This runs on **every** request, cheaply (signature + one indexed DB
lookup — no identity-provider call). That alone closes most holes because
the token is short-lived: a deactivated user's session dies within the
token window even if nothing else fires.

---

## 4. Refresh = the re-verification checkpoint

When the short access token expires and the client presents its refresh
token, **that is where the human is re-confirmed** — at most every
15–30 minutes, not on every request:

```
POST /api/auth/refresh:
  1. Validate + rotate the refresh token (reuse of a superseded token =
     suspected theft → the whole session is revoked, force re-login).
  2. Slack logins only: live users.info — deleted/gone → revoke, 401.
     (Local accounts have no external employment system; their liveness
     is the enabled requesters/admins row, re-checked next.)
  3. Re-check the whitelist (enabled requester or admin) — access removed
     → revoke, 401.
  4. Mint a fresh short access token.
```

So: someone removed from Slack (or disabled in `requesters`) loses web
access within one refresh cycle, automatically.

---

## 5. Live check at the dangerous moment

The truly sensitive action is **executing against production** — not
loading a page. So in addition to the refresh check, a **live check runs
right before an RW/DDL submit** (`routes_queries`):

```
POST /api/queries (and /queries/batch):
  ... grant checks ...
  if required tier is RW or DDL and provider == "slack":
      users.info(principal) — deleted/gone → revoke session, 401
  ... proceed to classify / submit ...
```

Not on every read — that would be slow and rate-limited. Where the blast
radius is real. Definitive "gone" answers fail closed; transport hiccups
fail open (the short TTL still bounds the window). Local logins skip the
Slack lookup — their gate is the whitelist row itself.

---

## 6. Instant kill switch (revocation)

For incidents ("revoke X right now"): revoke the session row —

```sql
UPDATE web_sessions SET revoked_at = NOW(), revoked_reason = '…'
WHERE slack_user_id = '<principal>' AND revoked_at IS NULL;
```

`current_user` checks liveness on every request (§3 step 3), so this cuts
access instantly without waiting for token expiry. Disabling the
`requesters` row (or the `local_users` row) additionally blocks re-login.

---

## 7. Putting the timers together

| Mechanism | Frequency | Catches |
|---|---|---|
| Access-JWT signature + exp | every request (cheap) | expired/forged tokens; short TTL closes most of the window |
| Session-row liveness | every request (cheap) | manual revocation, sign-out everywhere |
| Refresh: rotate + re-verify | every 15–30 min | user removed from Slack / whitelist, refresh-token theft (reuse detection) |
| Live check before RW/DDL | per dangerous action | someone removed *between* refreshes trying to write to prod |

**Never trust the frontend** — every real decision is made server-side in
`current_user` + the per-query grant check.

---

## 8. Frontend touch-points

- On load, the app calls `GET /api/me`. `401` → render the login screen.
- The login screen reads `GET /api/auth/providers` and renders a Slack
  button (`/api/auth/slack/start`, full redirect) and/or the local
  username/password form, per what's enabled.
- On `401` from any call mid-session, the client tries one silent
  `POST /api/auth/refresh`, then falls back to the login screen. (Login
  endpoints are exempt — a 401 there means bad credentials, not an
  expired session.)
- Sign out → `POST /api/auth/signout` (revokes the server-side session,
  not just the cookies).
