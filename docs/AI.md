# AI assistant

The web editor has an AI assistant. It writes, explains, optimizes and fixes
SQL, and it answers questions about the schema. It never runs SQL. A person
reads the SQL and runs it from the editor. The run goes through the normal
approval path.

The assistant is off by default. It works when an admin turns it on and enters
an API key.

## In the web editor

1. Click **AI** in the tab bar of a query tab. The button shows only when the
   assistant is on.
2. Choose a mode: Write SQL, Explain, Optimize, Fix or Chat.
3. Write the prompt and click **Send** (Ctrl+Enter).
4. Click **Insert** under a SQL block. The SQL goes in at the caret or over the
   selection. One undo takes it back.

Explain, Optimize, Fix and Chat send the selected SQL, or all of it. Write SQL
sends no editor SQL. Under each answer, a "Sent:" line says what left
QueryHub. The panel keeps one conversation for each tab while the page is open.

After an Insert, the next run of that tab sends `aiAssisted`. The approval
queue and History show an **AI** chip.

The admin form is **Admin → System → AI assistant** (super-admin). It edits the
settings and the providers, saves or clears an API key and runs a test call.
It deletes a provider only after an inline confirmation.

## Who can use it

The `ai_audience` table holds the rules. Edit them in the admin form, card
"Who can use it". Each rule names one of these:

| Kind | Names |
|---|---|
| Everyone | every signed-in user |
| Person | one principal (a Slack user id, or `local:<name>`) |
| Team | one team |
| Role | Super-admins, DBAs, Admins or Approvers |

A person can use the assistant when a rule names them. With no rule, nobody
can use it. Rules only open the assistant: there is no rule that closes it.
Mix as many people, teams and roles as you need. One save can add several
people, teams or roles. A rule that already exists stays as it is.

Migration 211 adds one rule: the super-admins. So the first calls are a pilot.

For a person outside the audience, `GET /api/ai/status` says `enabled: false`,
`POST /api/ai/complete` returns 403, and `POST /api/queries` drops
`aiAssisted`. The form shows how many enabled users the rules name now.

## What leaves QueryHub

On each call, QueryHub sends these items to the AI provider:

1. The prompt of the user.
2. The SQL from the editor: the selection, or the full text.
3. The table names of the database of the editor tab.
4. The table detail that the model asks for with its tools: columns, types,
   the CREATE script, indexes, keys, triggers and numeric statistics.

QueryHub never sends result rows, column values or server error text. A server
error goes out only when the user puts it in the prompt. Statistics that hold
column values (`most_common_vals`, `histogram_bounds`, SQL Server histogram
steps) never leave.

The tools work on the connection and the database of the editor tab only.
QueryHub checks the grant of the user before the call, with the same check as
the editor. The model gives only a table name.

## Providers

Each API key is one row in `ai_providers`. `kind` sets the protocol:

| kind | Protocol | Examples |
|---|---|---|
| `anthropic` | Anthropic Messages API, through the `anthropic` SDK | Claude models |
| `openai` | OpenAI Chat Completions API, through httpx | OpenAI, DeepSeek, together.ai, Ollama |

An Anthropic provider needs the optional extra: `pip install '.[ai]'`.

The base URL must use `https://`. QueryHub accepts plain `http://` only for
a loopback host, for example a local Ollama.

For an Anthropic model that supports it, QueryHub turns on the server-side
fallback (`fallbacks: "default"`). When the model declines a request, the API
runs the same request on a fallback model.

## Settings

Edit the settings in the admin form, or in System configuration under
"AI assistant". All keys are runtime settings.

| Key | Default | Meaning |
|---|---|---|
| `ai_enabled` | `off` | The assistant is on. |
| `ai_default_provider` | empty | The `ai_providers.id` for the default. |
| `ai_default_model` | empty | The default model. Empty is the first model of the provider. |
| `ai_user_model_choice` | `off` | A user can choose any enabled provider and model. |
| `ai_effort` | empty | Effort for Anthropic models: `low` to `max`. |
| `ai_instructions` | empty | Extra instructions for each call, for example house SQL style. |
| `ai_max_calls_per_hour` | `30` | Calls per user per hour. |
| `ai_timeout_sec` | `180` | Seconds for one call, with all its schema lookups. |
| `ai_schema_max_tables` | `200` | Table names in the first message of a call. |

## API

| Route | Who | What |
|---|---|---|
| `GET /api/ai/status` | whitelisted user | `enabled`, the models, what leaves |
| `POST /api/ai/complete` | whitelisted user | one call |
| `GET /api/admin/ai` | super-admin | settings, providers, presets |
| `PUT /api/admin/ai` | super-admin | change settings |
| `POST /api/admin/ai/providers` | super-admin | add a provider |
| `PUT /api/admin/ai/providers/{id}` | super-admin | change a provider, set or clear its key |
| `DELETE /api/admin/ai/providers/{id}` | super-admin | delete a provider |
| `POST /api/admin/ai/test` | super-admin | a small call to one provider |
| `POST /api/admin/ai/audience` | super-admin | add rules of who can use it: `kind` and `values` |
| `DELETE /api/admin/ai/audience/{id}` | super-admin | delete a rule |

`POST /api/ai/complete` takes `mode` (`generate`, `explain`, `optimize`,
`fix`, `chat`), `prompt`, `sql`, `messages` (earlier turns as text),
`connectionId`, `databaseId` and `model` (`<provider id>:<model>`).

The API key goes in through `apiKey` and never comes out. A provider shows
only `keySet`, `keySetBy` and `keySetAt`.

## The AI mark

`POST /api/queries` takes `aiAssisted`. QueryHub keeps the mark only while the
assistant is on. It goes to `requests.ai_assisted`. The approver sees
"written with the AI assistant" on the Slack card. History and the approval
queue show `aiAssisted`. The mark changes no rule.

## Audit

| Action | When |
|---|---|
| `ai_call` | each call: user, mode, provider, model, sizes, tools, tables read, status, time |
| `ai_provider_tested` | each test call from the admin form |
| `ai_provider_created`, `ai_provider_updated`, `ai_provider_deleted` | admin changes to a provider |
| `ai_settings_set` | admin changes to the settings |
| `ai_audience_added`, `ai_audience_removed` | admin changes to who can use it |
| `table_detail_read` | each `describe_table` lookup, with `via: ai` |

No row holds a prompt, SQL, an answer or a key. `ai_settings_set` records only
the length of `ai_instructions`.

## Key rotation

`scripts/rotate_master_key.py` re-encrypts `ai_providers.api_key_encrypted`
with the target passwords. See [KEY_ROTATION.md](KEY_ROTATION.md).
