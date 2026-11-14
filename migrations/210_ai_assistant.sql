-- 210: the AI assistant of the web editor (M13, 2026-11-14).
--
-- The assistant writes, explains and fixes SQL. It never runs SQL: a person
-- runs it from the editor, through the normal approval path. It is OFF until
-- an admin turns it on and enters an API key.
--
-- ai_providers holds one row per API key. `kind` picks the protocol:
-- `anthropic` (the Anthropic Messages API) or `openai` (any server that
-- speaks the OpenAI Chat Completions API: OpenAI, DeepSeek, together.ai,
-- Ollama). The key is Fernet ciphertext under the master key, the same as
-- the target passwords. scripts/rotate_master_key.py re-encrypts it. The API
-- never returns it.
--
-- requests.ai_assisted marks a request whose SQL came from the assistant. The
-- approver sees the mark. It changes no rule. Safe to run again.

CREATE TABLE IF NOT EXISTS ai_providers (
    id                serial PRIMARY KEY,
    name              text NOT NULL UNIQUE,
    kind              text NOT NULL CHECK (kind IN ('anthropic', 'openai')),
    base_url          text,
    models            text[] NOT NULL DEFAULT '{}',
    supports_tools    boolean NOT NULL DEFAULT true,
    enabled           boolean NOT NULL DEFAULT true,
    api_key_encrypted text,
    key_set_by        text,
    key_set_at        timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE requests ADD COLUMN IF NOT EXISTS ai_assisted boolean NOT NULL DEFAULT false;

INSERT INTO bot_config (key, value, description) VALUES
    ('ai_enabled', 'off',
     'The AI assistant of the web editor. It works only when this is on AND the default provider has an API key.'),
    ('ai_default_provider', '',
     'The id of the ai_providers row that the assistant uses when the user does not choose one.'),
    ('ai_default_model', '',
     'The model that the assistant uses when the user does not choose one. Empty means the first model of the default provider.'),
    ('ai_user_model_choice', 'off',
     'When on, a user can choose any enabled provider and model. When off, everyone uses the default.'),
    ('ai_effort', '',
     'The effort level for Anthropic models: low, medium, high, xhigh or max. Empty means the model default.'),
    ('ai_instructions', '',
     'Extra instructions that the assistant gets with every request, for example house SQL style.'),
    ('ai_max_calls_per_hour', '30',
     'Maximum assistant requests per user per hour.'),
    ('ai_timeout_sec', '180',
     'Maximum seconds for one assistant request, with all its schema lookups.'),
    ('ai_schema_max_tables', '200',
     'Maximum table names that the assistant gets at the start of a request. It can look up more with its tools.')
ON CONFLICT (key) DO NOTHING;
