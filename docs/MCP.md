# The MCP door: Claude Code and QueryHub

A person asks Claude for data. Claude finds the tables, writes the SQL and
submits it to QueryHub. QueryHub classifies the statement, applies the rules of
the person and sends a risky statement to a DBA. Claude reads the status, the
row count and the column names. The person types no SQL.

Claude Code connects to the portal (the IDP panel), not to QueryHub. The
portal sends each tool call to `POST /mcp` with its identity assertion.

The door is off until an operator turns it on. Every key is in
`docs/CONFIGURATION.md` under "MCP door".

## 1. How the pieces fit

| Piece | What it is | Where it lives |
|---|---|---|
| `/mcp` | MCP over Streamable HTTP. Only the portal calls it. | `web/routes_mcp.py` |
| The MCP server of the portal | Answers `initialize` and `tools/list` for Claude Code, lists each tool as `queryhub_<name>`, and sends each tool call to `/mcp`. It is not in this repository. | the portal |
| Catalog file | The tool list that the portal embeds | `docs/mcp-tools.idp.json` |
| Tools | `list_connections`, `describe_database`, `classify_sql`, `submit_query`, `query_status`, `fetch_result` | `mcp_server/tools.py` |
| Catalog | The tool names, descriptions and argument shapes | `mcp_server/catalog.py` |
| `/sql claude` | The Slack command that says how to connect | `slack_app/claude_cmd.py` |

`mcp_door_enabled` is the switch: `off` (the default) closes the door.

This door is the only way to MCP. The stdio server that read
`QH_MCP_PRINCIPAL` is gone, and so is its key `mcp_enabled`: QueryHub reads
it no more. The new key is off on every install, so a deploy never opens the
door, also where `mcp_enabled` was `on`.

## 2. What a person does

1. Open QueryHub in the portal and select Connect Claude Code on the Welcome
   tab. `/sql claude` in Slack sends you there too.
2. Create a personal access token with the QueryHub permission `mcp:use`
   and no other QueryHub permission. The page shows how.
3. Run the `claude mcp add` command that the page shows, in a terminal.
   Claude Code then connects to the portal with your token.
4. Ask Claude a question about your data.

The token is the credential of the portal. QueryHub never sees it. When the
token expires or you revoke it in the portal, Claude Code stops. Make a new
token and run the command again.

To remove QueryHub from Claude Code, run `claude mcp remove` with the name of
the server.

### What `/api/me` and `/sql claude` say

`/api/me` sends `claudeMcp`, and the Welcome tab in the portal shows Connect
Claude Code from it:

| `claudeMcp` | When |
|---|---|
| `{"mode": "idp"}` | `mcp_door_enabled` is `on` and `idp_assertion_enabled` is `on`. The portal shows its own steps. |
| no key | Every other case |

`/sql claude` follows the same answer. It sends the person to Connect Claude
Code in the portal. While the identity assertions are off, it names
`idp_assertion_enabled`. It has no subcommand: QueryHub keeps no row for a
token of the portal.

## 3. Identity

- Claude Code sends its personal access token to the portal. The portal checks
  the token and answers `initialize` and `tools/list` itself. For a tool call,
  it also checks that the token and the person hold the QueryHub permission
  `mcp:use`.
- The REST routes of the portal do not accept `mcp:use`. A token that holds
  only `mcp:use` cannot read results through them, so the MCP rules of this
  door apply to every call it makes. A token that also holds `query:submit`
  can use the REST routes, and the MCP rules do not apply there. The portal
  cannot tell a token from a browser session, so the person chooses the
  permissions of the token.
- For each tool call, the portal sends `POST /mcp` to QueryHub with an
  `x-idp-assertion` header. It is the same Ed25519 assertion as on the proxied
  `/api` routes (`docs/AUTH.md` section 1.2), bound to `POST`, the path `/mcp`
  and the exact body.
- QueryHub finds the person with the same helper as the proxied `/api` routes
  (`deps.assertion_claims`). The rules and the refusal codes are the same: the
  key, the lifetime, the binding, the single-use `jti`, the allowed domain and
  exactly one `requesters` or `admins` row.
- The person must also be on the whitelist: an enabled requester, or an admin.
  The resolver also finds a disabled row, and this door has no sign-in that
  refuses it. So the door refuses it with 403 `not_whitelisted`.
- A request with no assertion gets 401 `unauthenticated`, and the message
  names Connect Claude Code in the portal. A request with an assertion and a
  bearer token gets 400 `ambiguous_credentials`, before QueryHub reads the
  assertion.
- QueryHub keeps no session. Each call carries its own proof, which lives 60
  seconds. A disabled QueryHub account stops the person at the next call. A
  revoked token or a removed `mcp:use` stops the person in at most 15
  minutes: the portal keeps a short session for the token until then.

## 4. The web routes and `/mcp`

- `/mcp` accepts only an assertion.
- The own-UI switch (`web_own_ui_super_admin_only`) does not close `/mcp`. MCP
  follows Slack and the portal: while they serve a person, MCP serves them too.
  `mcp_door_enabled` closes MCP.
- The server decides `origin=mcp`. The client cannot change it.

### The transport

- `POST /mcp` takes one JSON-RPC message. A list of messages gets 400
  `Invalid request`. A request gets one JSON answer. A notification or a
  response gets `202 Accepted`.
- `GET /mcp` and `DELETE /mcp` answer 405. QueryHub sends no message of its
  own and keeps no MCP session. It sets no `Mcp-Session-Id`.
- The door knows `ping`, `tools/list` and `tools/call`. The portal answers
  `initialize` itself. Another method gets the JSON-RPC error -32601.
- The protocol versions are `2025-06-18`, `2025-03-26` and `2024-11-05`.
  Another `MCP-Protocol-Version` header gets 400.
- A tool result is one text block with JSON. A refusal is a result with
  `isError: true` and the JSON `{"error": "<code>", "message": "..."}`, so
  Claude can act on it. A refused tool call is HTTP 200.
- A refusal before QueryHub reads a message is a JSON-RPC error with
  `"id": null`, the code -32000, a message and `data.code`, with a status of
  400, 401, 403 or 503 (section 6). The portal turns it into a tool result
  with `isError: true`, so the model reads the reason.
- Every answer carries `x-queryhub-catalog`: the version of the catalog
  (section 7.2). The portal compares it with the version of its copy.

The door gets this body from the portal, with the bare tool name:

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "list_connections", "arguments": {}}}
```

## 5. What the rules are

- **Databases.** A person sees only the databases they may use. One function
  decides the list for the web UI, the MCP tools and the submit check
  (`db_scope.py`). A database outside the list gets one answer: "You have no
  access to database `X` on `alias`." The answer is the same when the database
  does not exist.
- **The database of a submit.** `submit_query` needs `database`. Slack and the
  web UI fall back to the default database of the connection. The person sees
  that choice in the form. A program never chose it, so a call without
  `database` gets `bad_arguments`.
- **Classification.** QueryHub classifies on the server. `classify_sql` is
  optional and adds no security. `submit_query` classifies again, refuses a
  blocked statement, applies `mcp_max_tier`, and then runs the normal checks.
- **Approval.** An RO request from MCP follows the normal RO rules. An RW or DDL
  request always waits for a person. A waiver, a super-admin self-approval and
  the fingerprint cache do not apply, so such a request always needs a
  `justification`: the approver reads it. The audit row of such a request
  carries `human_approval_forced`. The approver is a second person: the person
  who sent the request cannot approve it, also when they are an admin
  (`admins.own_mcp_write`).
- **The approval message.** For a request from MCP, the message says "Claude
  wrote this statement", shows the justification and says when no plan exists.
- **The request link.** Every request comes back with `requestUrl`, for example
  `https://portal.example.com/queryhub?request=41`. The person opens it in the
  portal and lands on their own request, as a tab of their workspace. The tab
  shows the SQL, "waiting for a DBA", and the result when the request runs.
  Claude shows the link. The approvers get the request in Slack and in their
  queue, so the person forwards nothing. The link needs `mcp_portal_url`.
  Without it, no link comes back. QueryHub shows a request only to its owner.
- **Current session.** The Welcome tab in the portal lists each request from
  Claude in Current session until it ended and the person opened it. A tab
  that shows an ended request sends `POST /api/queries/<id>/seen`. QueryHub
  keeps the first time (`requests.requester_seen_at`, migration 203), and
  `GET /api/history` returns it as `seenAt`. A request that has not ended gets
  409 `not_ended`.
- **Results.** By default (`mcp_result_mode` = `metadata_only`), Claude reads no
  row. It reads the status, the row count and the column names. The person opens
  the result in the QueryHub web UI, in the tab that `requestUrl` opens. For a read,
  `rowCount` stops at the row limit (`max_rows`, default 1000). Then `truncated`
  is true and the real number is higher. For a write, `rowCount` is the rows it
  changed. A result of `COUNT(*)` is a row too, so Claude sees `rowCount: 1`
  and not the number. QueryHub cleans an error text again, since a database can
  echo a value in an error. That clean hides the echoed value with PII masking
  off too.
  Set `mcp_result_mode` to `masked_rows` to send the masked rows as well. The
  rows then reach the model. A result with several statements is not readable
  through MCP in that mode. QueryHub refuses a result that a super-admin ran
  without masking (`unmasked_result`). The person opens it in the web UI.
- **The wait.** `submit_query` waits for the result of an auto-approved
  request. The default and the maximum are 20 seconds. Two calls of at most
  30 seconds each sit in front of the door: the portal to QueryHub, and the
  call inside the portal that carries the person. The portal cancels its call
  to QueryHub after 25 seconds. So the wait counts from the start of the call.
  The checks, the pre-flight plan and the notices before the wait are part of
  it. After the wait, Claude polls `query_status`. A call that asks for a
  longer wait waits 20 seconds.
- **A second submit.** A client that gave up on a slow call can send the same
  submit again. While the first request waits or runs, `submit_query` answers
  with that request: its `requestId`, `requestUrl`, `status`, a `decision` and
  `"duplicate": true`, with a note to poll `query_status`. QueryHub matches the
  same person, connection, database and statement exactly. It makes no second
  request. If the first request ended in between, the answer is the refusal
  `duplicate`. A statement with a password literal always gets that refusal.
  QueryHub stores it masked, so two statements that differ only in the
  password look the same.
- **Load.** One person may make `mcp_rate_limit_per_minute` calls in a minute.
  One process runs at most 16 calls at once. Every call that waits holds a
  thread for up to 20 seconds. The calls use threads of their own, so they do
  not take the threads of the web UI.
- **Logs.** A log line names the door (`door=idp`), the person, the tool and
  the outcome. It never holds the arguments. The audit log of the portal keeps
  the body of each `POST /api/queryhub/mcp/call`, the SQL included. It does the
  same for a query sent from the web UI.

## 6. Errors

A refused tool call is a result with `isError: true`. Its JSON has an error
`code` and a `message`. A refusal before QueryHub reads a message has an HTTP
status. The portal turns it into a tool result too.

| Code | Meaning | Action |
|---|---|---|
| `disabled` (HTTP 503) | `mcp_door_enabled` is `off` | Ask an operator to enable it |
| `unauthenticated` (HTTP 401) | No assertion, or an assertion that QueryHub refused | Connect Claude Code through the portal. An operator reads the log line `idp assertion refused: <code>`. |
| `not_onboarded`, `bad_domain`, `ambiguous_address` (HTTP 403) | The address of the person has no account, is outside the domain, or belongs to two accounts (`docs/AUTH.md` section 1.2) | Ask an admin |
| `not_whitelisted` (HTTP 403, or a result) | The person has no QueryHub access | Ask the DBA team |
| `ambiguous_credentials` (HTTP 400) | An assertion and a bearer token in one request | Send one of them |
| `unknown_connection` | No such connection, or no grant | Call `list_connections` |
| `no_access` (`database` from `submit_query`) | The database is not in the person's list | Call `list_connections` |
| `server` | The connection is gone, is a replica, or the grant ended | Read the message |
| `justification` | An RW or DDL statement has no reason | Give a `justification` |
| `query` | The statement is empty, too long or not allowed | Read the message and change the statement |
| `no_catalog` | The first snapshot of the database has not run | Wait for the hourly job |
| `no_match` | No catalogued table has the name that `table` gives | Call `describe_database` without `table` |
| `blocked` | The safety rules refuse the statement | Change the statement |
| `tier_refused` | The statement needs more than `mcp_max_tier` | Use Slack or the web UI |
| `tier_exceeds` | The grant is below the needed tier | Ask for a grant |
| `duplicate` | An open request holds this statement, or a DBA asked for changes to it | Change the statement, or cancel the request |
| `rate_limited`, `busy` | Too many calls | Wait a minute |
| `rate_limit` | The person has too many open requests | Wait for one to end |
| `kill_switch`, `draining` | QueryHub takes no new request now | Try again later |
| `approval_rule` (from the portal) | An approval rule of the portal covers `queryhub:mcp:use`, so the portal held the call (section 7.1) | Ask a portal admin to remove the rule |
| `bad_arguments`, `unknown_tool` | The call has the wrong shape, for example no `database` for `submit_query` | Fix the call |
| `not_ready`, `not_found`, `expired` | The request has no result to read | Poll `query_status` |
| `multi_statement`, `unmasked_result` | In `masked_rows` mode: the request stored one result for each statement, or a super-admin ran it without masking | Open the result in the web UI |

The answer of `list_connections` has `resultMode`. It tells Claude what it reads
of a result.

## 7. Set it up

### 7.1 Turn the door on

1. Turn on the identity assertions of the portal (`docs/AUTH.md` section 1.2).
   The proxied `/api` routes need them already.
2. Set `mcp_portal_url` to the address of QueryHub in the portal, for example
   `https://portal.example.com/queryhub`. Every request then comes back with a
   link.
3. Turn the door on with the DB change procedure of `CLAUDE.md`: a dry run,
   a transaction and an `audit_log` row. For example:

   ```sql
   -- 1. Show first.
   SELECT key, value FROM bot_config
    WHERE key IN ('mcp_door_enabled', 'mcp_max_tier', 'mcp_portal_url');

   -- 2. Apply, in one transaction, with the audit row.
   BEGIN;
   UPDATE bot_config SET value = 'on' WHERE key = 'mcp_door_enabled';
   UPDATE bot_config SET value = 'ro' WHERE key = 'mcp_max_tier';
   INSERT INTO audit_log (actor_slack_id, actor_name, action, details)
   VALUES ('<operator id>', '<operator name>', 'config_set',
           '{"keys": ["mcp_door_enabled", "mcp_max_tier"], "reason": "MCP pilot"}');
   COMMIT;
   ```

   The trigger of migration 204 also writes a `security_config_changed` row
   for each change of `mcp_door_enabled`, `mcp_max_tier` and
   `mcp_result_mode`. The row above adds who asked for the change and why.
4. In the portal, turn on its MCP route and give it the catalog file of
   section 7.2. The portal sends each tool call to `<QueryHub address>/mcp`.
   Let a reverse proxy in front of QueryHub pass `POST /mcp` with no
   buffering of the answer.
5. Give `mcp:use` to a small pilot role in the portal. Start with `ro` for
   one week. Then set `mcp_max_tier` to `rw`, then to `ddl`. No restart needed.

Do not put an approval rule of the portal on `queryhub:mcp:use`. Every tool
call goes to the portal route `POST /api/queryhub/mcp/call`, so the rule holds
each call, also `query_status`, and Claude Code cannot work. QueryHub approves
the statements itself: a person approves every RW and DDL request. If such a
rule exists:

- The portal holds the call and Claude gets the error `approval_rule`. Its
  message tells the person to ask a portal admin to remove the rule.
- A held call never runs, also after an approver approves it in the portal.
  The portal refuses it with `queryhub.mcp_approval_replay`. So no request
  opens hours later with nobody waiting for it.

To close the door at once, set `mcp_door_enabled` to `off`. To stop one person at
once, disable their QueryHub account. A revoked token or a removed `mcp:use`
stops the person in at most 15 minutes.

### 7.2 The catalog file

The portal lists the tools from a copy of `docs/mcp-tools.idp.json`. The file
holds the six tools with their bare names, the descriptions and the argument
shapes. The portal answers `initialize` with its own instructions, so the
rules of QueryHub are in these descriptions.

The portal lists each tool as `queryhub_<name>`. So a description in the file
names a tool that way, for example `queryhub_query_status`. The answers of the
door do the same.

`version` is the sha256 of the tool list as canonical JSON (sorted keys, no
spaces, ASCII). The portal reads it and does not compute it again. Every
answer of the door sends the version that QueryHub runs, in
`x-queryhub-catalog`.

After a change to a tool, make the file again and copy it to the portal:

```
python -m queryhub.mcp_server.catalog --door idp > docs/mcp-tools.idp.json
```

The command needs no database and no environment. A test fails while the file
differs from its output.

## 8. What the test suite cannot check

- The door with a real portal and a real Claude Code. The suite signs real
  assertions with a test key and runs them through the real `verify()`, but
  the MCP server of the portal is not here. Try it once in a test environment
  before the pilot.
- How a reverse proxy in front of the host treats a long `POST /mcp`. Check
  it on the host.
- An RO query on `pg_database` or `sys.databases` can list database names. Run
  the two queries with an RO role on each engine and decide with the operator.
