"""The AI assistant of the web editor (M13).

The assistant writes, explains, optimizes and fixes SQL, and it answers
questions about the schema. It never runs SQL. A person reads what it wrote
and runs it from the editor, through the normal approval path. The request of
that run carries the mark `requests.ai_assisted`, which the approver sees and
which changes no rule.

What leaves QueryHub on one call:

- the prompt of the user and the SQL from the editor.
- the table names of the database of the editor tab.
- the table detail that the model asks for through its tools, from
  `schema_tools`: columns, types, the CREATE script, indexes, keys, triggers
  and numeric statistics.

Result rows, column values and server error text never leave. The tools are
bound on the server to the connection and the database of the tab, after the
same grant check as the editor, so the model cannot name another database. A
server error goes out only when the user puts it in the prompt.

Providers: one `ai_providers` row per API key. `kind` picks the protocol.
`anthropic` goes through the official `anthropic` SDK (an optional extra,
imported only on a call). `openai` goes through httpx to
`{base_url}/chat/completions`, which OpenAI, DeepSeek, together.ai, Ollama and
many others speak. The key is Fernet ciphertext under the master key. No
function here returns it or logs it.

Each call writes one audit row, `ai_call`: who, the mode, the provider, the
model, the sizes, the tools it used and the tables it read. The row holds no
prompt, no SQL and no answer.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import admins, audit, crypto, db, requesters, schema_tools
from . import config as cfg

log = logging.getLogger(__name__)

#: What a call can ask the assistant to do.
MODES = ("generate", "explain", "optimize", "fix", "chat")

#: Provider protocols.
KINDS = ("anthropic", "openai")

#: Effort levels for Anthropic models. Empty means the default of the model.
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")

#: Anthropic models that take server-side fallbacks on a refusal.
FALLBACK_MODELS = frozenset({
    "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
    "claude-fable-5-1", "claude-fable-5-2",
})
FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: The output cap of one model reply. A non-streaming call stays inside the
#: HTTP timeout of the SDK at this size.
MAX_TOKENS = 16_000

#: Model turns in one call. Each tool round is one turn.
MAX_ROUNDS = 8

#: Schema lookups in one call. Each `describe_table` opens a connection to
#: the target with its read login.
MAX_TOOL_CALLS = 12

#: The longest tool answer that goes back to the model, in characters.
MAX_TOOL_RESULT_CHARS = 60_000

#: Input caps, in characters.
MAX_PROMPT_CHARS = 8_000
MAX_SQL_CHARS = 60_000
MAX_TURN_CHARS = 20_000
MAX_TURNS = 20
MAX_INSTRUCTIONS_CHARS = 8_000

#: Admin input caps.
MAX_NAME_CHARS = 60
MAX_MODELS = 30
MAX_MODEL_CHARS = 200
MAX_KEY_CHARS = 500

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+-]+$")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})

_ENGINE_LABELS = {
    "postgres": "PostgreSQL",
    "mssql": "SQL Server (T-SQL)",
    "clickhouse": "ClickHouse",
    "athena": "Amazon Athena (Trino SQL)",
}
_READ_ONLY_ENGINES = frozenset({"clickhouse", "athena"})

#: Starting points for the admin form. Only the Anthropic model names are
#: known here. For the others the admin types the model names.
PRESETS = (
    {"name": "Anthropic", "kind": "anthropic", "baseUrl": None,
     "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]},
    {"name": "OpenAI", "kind": "openai", "baseUrl": "https://api.openai.com/v1",
     "models": []},
    {"name": "DeepSeek", "kind": "openai", "baseUrl": "https://api.deepseek.com",
     "models": []},
    {"name": "together.ai", "kind": "openai", "baseUrl": "https://api.together.xyz/v1",
     "models": []},
    {"name": "Other", "kind": "openai", "baseUrl": "", "models": []},
)

#: What the status endpoint says leaves QueryHub on a call.
SENDS = ("prompt", "sql", "tableNames", "tableDetail")


class AIError(Exception):
    """A refusal or a failure that the web route turns into an HTTP answer.
    `code` is short and log-safe. `message` is for the user."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# --- settings ---------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    enabled: bool
    default_provider: int | None
    default_model: str
    user_model_choice: bool
    effort: str
    instructions: str
    max_calls_per_hour: int
    timeout_sec: int
    schema_max_tables: int


def _bounded_int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = cfg.get_int(key, default)
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def settings() -> Settings:
    """The `ai_*` keys of bot_config. They are runtime settings."""
    raw_provider = cfg.get_setting("ai_default_provider", "").strip()
    effort = cfg.get_setting("ai_effort", "").strip().lower()
    return Settings(
        enabled=cfg.get_bool("ai_enabled", False),
        default_provider=int(raw_provider) if raw_provider.isdigit() else None,
        default_model=cfg.get_setting("ai_default_model", "").strip(),
        user_model_choice=cfg.get_bool("ai_user_model_choice", False),
        effort=effort if effort in EFFORTS else "",
        instructions=cfg.get_setting("ai_instructions", "")[:MAX_INSTRUCTIONS_CHARS],
        max_calls_per_hour=_bounded_int("ai_max_calls_per_hour", 30, 1, 1000),
        timeout_sec=_bounded_int("ai_timeout_sec", 180, 10, 600),
        schema_max_tables=_bounded_int("ai_schema_max_tables", 200, 0,
                                       schema_tools.MAX_TABLES),
    )


# --- providers --------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    id: int
    name: str
    kind: str
    base_url: str | None
    models: tuple[str, ...]
    supports_tools: bool
    enabled: bool
    key_set: bool
    key_set_by: str | None = None
    key_set_at: datetime | None = None

    @property
    def usable(self) -> bool:
        return self.enabled and self.key_set and bool(self.models)

    @property
    def host(self) -> str:
        if not self.base_url:
            return "api.anthropic.com" if self.kind == "anthropic" else ""
        return urlsplit(self.base_url).hostname or ""


# The ciphertext is not in this list. Only `_key_of` reads it.
_PROVIDER_COLS = ("id, name, kind, base_url, models, supports_tools, enabled, "
                  "api_key_encrypted IS NOT NULL AS key_set, key_set_by, key_set_at")


def _provider(row: dict) -> Provider:
    return Provider(
        id=row["id"], name=row["name"], kind=row["kind"], base_url=row["base_url"],
        models=tuple(row["models"] or ()), supports_tools=bool(row["supports_tools"]),
        enabled=bool(row["enabled"]), key_set=bool(row["key_set"]),
        key_set_by=row.get("key_set_by"), key_set_at=row.get("key_set_at"))


def providers() -> list[Provider]:
    return [_provider(r) for r in
            db.fetch_all(f"SELECT {_PROVIDER_COLS} FROM ai_providers ORDER BY id")]


def _key_of(provider_id: int) -> str:
    row = db.fetch_one("SELECT api_key_encrypted FROM ai_providers WHERE id = %s",
                       (provider_id,))
    if not row or not row["api_key_encrypted"]:
        raise AIError(503, "no_key", "The AI provider has no API key. An admin "
                                     "must enter one.")
    return crypto.decrypt(row["api_key_encrypted"])


@dataclass(frozen=True)
class Choice:
    provider: Provider
    model: str

    @property
    def id(self) -> str:
        return model_id(self.provider.id, self.model)


def model_id(provider_id: int, model: str) -> str:
    """The id of one provider and model, as the API names it: `<id>:<model>`."""
    return f"{provider_id}:{model}"


def default_choice(s: Settings, provs: list[Provider]) -> Choice | None:
    """The default provider and model, or None when the default provider is
    missing, off, or has no key or no model."""
    p = next((p for p in provs if p.id == s.default_provider), None)
    if p is None or not p.usable:
        return None
    model = s.default_model if s.default_model in p.models else p.models[0]
    return Choice(p, model)


def choices(s: Settings, provs: list[Provider]) -> list[Choice]:
    """The models that a user can choose. Only the default when the admin
    does not let users choose."""
    d = default_choice(s, provs)
    if d is None:
        return []
    if not s.user_model_choice:
        return [d]
    out = [Choice(p, m) for p in provs if p.usable for m in p.models]
    out.sort(key=lambda c: c.id != d.id)            # the default first
    return out


def readiness(s: Settings, provs: list[Provider]) -> tuple[bool, str | None]:
    """(ready, why not). Ready means a user sees the assistant."""
    if not s.enabled:
        return False, "The assistant is off."
    if s.default_provider is None:
        return False, "Choose a default provider."
    p = next((p for p in provs if p.id == s.default_provider), None)
    if p is None:
        return False, "The default provider does not exist."
    if not p.enabled:
        return False, "The default provider is off."
    if not p.key_set:
        return False, "The default provider has no API key."
    if not p.models:
        return False, "The default provider has no model."
    return True, None


def is_enabled_for(uid: str) -> bool:
    """On, with a usable default, and open for this user."""
    s = settings()
    return (s.enabled and default_choice(s, providers()) is not None
            and in_audience(uid))


def status(uid: str) -> dict:
    """What the editor needs to show the assistant. Nothing else when off, or
    when the assistant is not open for this user."""
    s = settings()
    provs = providers()
    d = default_choice(s, provs)
    if not s.enabled or d is None or not in_audience(uid):
        return {"enabled": False}
    opts = choices(s, provs)
    return {
        "enabled": True,
        "default": d.id,
        "models": [{"id": c.id, "provider": c.provider.name, "model": c.model}
                   for c in opts],
        "userModelChoice": s.user_model_choice and len(opts) > 1,
        "modes": list(MODES),
        "sends": list(SENDS),
        "callsPerHour": s.max_calls_per_hour,
    }


# --- the rate limit -----------------------------------------------------------

# Process-local, like the MCP door. The web app is one process, and a restart
# that empties the window costs at most one hour of calls.
_calls: dict[str, deque] = {}
_calls_lock = threading.Lock()


def take_slot(uid: str, limit: int, now: float | None = None) -> bool:
    """Count one call for `uid`. False when the user already made `limit`
    calls in the last hour."""
    now = time.monotonic() if now is None else now
    with _calls_lock:
        q = _calls.setdefault(uid, deque())
        while q and now - q[0] >= 3600:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def reset_rate_limit() -> None:
    with _calls_lock:
        _calls.clear()


# --- who can use it -----------------------------------------------------------

#: The rule kinds of `ai_audience`. A rule only opens the assistant.
AUDIENCE_KINDS = ("everyone", "person", "team", "role")

#: Values one write may add.
MAX_AUDIENCE_VALUES = 100

#: Role values -> the words of the admin form.
AUDIENCE_ROLES = {
    "super_admin": "Super-admins",
    "dba": "DBAs",
    "admin": "Admins",
    "approver": "Approvers",
}

_PRINCIPAL_RE = re.compile(r"^(?:[UW][A-Z0-9]{8,}|local:[a-z0-9._-]{1,64})$")


def audience_rules() -> list[dict]:
    return db.fetch_all("SELECT id, kind, value, created_by, created_at "
                        "FROM ai_audience ORDER BY kind, value, id")


def _team_ids(uid: str) -> set[int]:
    """The teams of a principal, from the access model tables."""
    rows = db.fetch_all(
        "SELECT tm.team_id FROM team_member tm "
        "  JOIN team t ON t.id = tm.team_id AND NOT t.is_deleted "
        "  JOIN principal_identity i ON i.principal_id = tm.principal_id "
        "   AND i.provider = 'slack' AND NOT i.is_deleted "
        " WHERE i.external_id = %s AND NOT tm.is_deleted",
        (uid,))
    return {int(r["team_id"]) for r in rows if r.get("team_id") is not None}


def _has_role(uid: str, role: str) -> bool:
    if role == "super_admin":
        return admins.is_super_admin(uid)
    if role == "dba":
        return admins.is_super_admin(uid) or admins.is_dba(uid)
    if role == "admin":
        return admins.is_admin(uid)
    if role == "approver":
        return admins.holds_approval_role(uid)
    return False


def in_audience(uid: str, rules: list[dict] | None = None) -> bool:
    """True when a rule names `uid`. With no rule, nobody passes.

    The team list is read once, and only when a person and an everyone rule
    did not already decide.
    """
    rules = audience_rules() if rules is None else rules
    teams: set[int] | None = None

    def names(rule: dict) -> bool:
        nonlocal teams
        kind, value = rule["kind"], rule["value"]
        if kind == "everyone":
            return True
        if kind == "person":
            return value == uid
        if kind == "team":
            if teams is None:
                teams = _team_ids(uid)
            return value.isdigit() and int(value) in teams
        if kind == "role":
            return _has_role(uid, value)
        return False

    # Cheap kinds first, so a team read happens only when it can decide.
    order = {"everyone": 0, "person": 1, "role": 2, "team": 3}
    return any(names(r) for r in sorted(rules, key=lambda r: order.get(r["kind"], 9)))


def audience_count(rules: list[dict]) -> int | None:
    """How many enabled requesters and active admins pass the rules now.
    None when the count fails: the form then says nothing rather than 0."""
    try:
        people = set(requesters.list_enabled_ids())
        people |= {a["slack_user_id"] for a in admins.list_active() if a.get("slack_user_id")}
        return sum(1 for uid in people if in_audience(uid, rules))
    except Exception:                               # noqa: BLE001
        log.warning("ai: audience count failed", exc_info=True)
        return None


def clean_audience(kind: Any, values: Any) -> tuple[str, list[str]]:
    """(kind, values) of one write, checked. `everyone` takes no value. The
    other kinds take one or more values, and each must exist."""
    if kind not in AUDIENCE_KINDS:
        raise ValueError("The kind must be everyone, person, team or role.")
    if kind == "everyone":
        return kind, [""]
    raw = values if isinstance(values, list) else [values]
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    if not out:
        raise ValueError("Choose at least one.")
    if len(out) > MAX_AUDIENCE_VALUES:
        raise ValueError(f"Add at most {MAX_AUDIENCE_VALUES} at a time.")
    if kind == "person":
        bad = [v for v in out if not _PRINCIPAL_RE.match(v)]
        if bad:
            raise ValueError(f"'{bad[0][:40]}' is not a person.")
    elif kind == "team":
        ids = [int(v) for v in out if v.isdigit()]
        rows = db.fetch_all("SELECT id FROM team WHERE id = ANY(%s) AND NOT is_deleted",
                            (ids,)) if ids else []
        known = {str(r["id"]) for r in rows}
        missing = [v for v in out if v not in known]
        if missing:
            raise ValueError(f"Team {missing[0][:40]} does not exist.")
    else:
        bad = [v for v in out if v not in AUDIENCE_ROLES]
        if bad:
            raise ValueError("The role must be super_admin, dba, admin or approver.")
    return kind, out


def add_audience(cur, kind: str, values: list[str], uid: str,
                 actor_name: str | None) -> int:
    """Add one rule per value and one audit row per new rule. A rule that
    already exists stays as it is. Returns how many rules are new."""
    added = 0
    for value in values:
        cur.execute(
            "INSERT INTO ai_audience (kind, value, created_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (kind, value) DO NOTHING RETURNING id", (kind, value, uid))
        row = cur.fetchone()
        if row is None:
            continue
        audit.log_in(cur, None, uid, actor_name, "ai_audience_added",
                     {"rule_id": row["id"], "kind": kind, "value": value})
        added += 1
    return added


def remove_audience(cur, rule: dict, uid: str, actor_name: str | None) -> None:
    cur.execute("DELETE FROM ai_audience WHERE id = %s", (rule["id"],))
    audit.log_in(cur, None, uid, actor_name, "ai_audience_removed",
                 {"rule_id": rule["id"], "kind": rule["kind"], "value": rule["value"]})


def audience_view(rules: list[dict]) -> dict:
    return {
        "rules": [{"id": r["id"], "kind": r["kind"], "value": r["value"],
                   "createdBy": r.get("created_by")}
                  for r in rules],
        "count": audience_count(rules),
        "roles": dict(AUDIENCE_ROLES),
    }


# --- the tools of the model ---------------------------------------------------

TOOLS = (
    {"name": "list_tables",
     "description": (
         "List the tables of the database that the user's editor tab uses. "
         "Without `table`, it gives the table names. With `table`, it gives "
         "the tables whose name contains that text, with their columns and "
         "column types."),
     "schema": {"type": "object",
                "properties": {"table": {
                    "type": "string",
                    "description": "Part of a table name. Leave it empty to "
                                   "list the table names."}},
                "additionalProperties": False}},
    {"name": "describe_table",
     "description": (
         "Describe one table of the database that the user's editor tab uses: "
         "columns, types, the CREATE script, indexes, primary and foreign "
         "keys, triggers and numeric statistics. It never gives row data."),
     "schema": {"type": "object",
                "properties": {"table": {
                    "type": "string",
                    "description": "The table name: `table` or `schema.table`."}},
                "required": ["table"],
                "additionalProperties": False}},
)

# The schema tools name MCP tools in their hints. The assistant has other
# names for the same lookups.
_TOOL_NAMES = {"describe_database": "list_tables"}


def _named(name: str) -> str:
    return f"`{_TOOL_NAMES.get(name, name)}`"


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


class Toolbox:
    """The tools of one call, bound to one connection and one database that
    the grant check let in. The model gives only a table name."""

    def __init__(self, uid: str, actor_name: str | None, target, database: str):
        self.uid = uid
        self.actor_name = actor_name
        self.target = target
        self.database = database
        self.calls: list[dict] = []

    def run(self, name: str | None, args: Any) -> tuple[str, bool]:
        """(the answer as JSON text, is an error)."""
        if len(self.calls) >= MAX_TOOL_CALLS:
            return _json({"error": "limit", "message": (
                f"This request already made {MAX_TOOL_CALLS} schema lookups. "
                f"Answer with what you have.")}), True
        if not isinstance(args, dict):
            return _json({"error": "bad_arguments",
                          "message": "The arguments must be a JSON object."}), True
        table = str(args.get("table") or "").strip()[:schema_tools.MAX_NAME_CHARS]
        self.calls.append({"tool": str(name)[:40], "table": table[:120]})
        try:
            if name == "list_tables":
                out = (schema_tools.tables_matching(self.target, self.database, table,
                                                    named=_named)
                       if table else schema_tools.table_names(self.target, self.database))
            elif name == "describe_table":
                out = schema_tools.table_detail(self.uid, self.actor_name, self.target,
                                                self.database, table, via="ai",
                                                named=_named)
            else:
                return _json({"error": "unknown_tool",
                              "message": "Use list_tables or describe_table."}), True
        except schema_tools.ToolError as e:
            return _json({"error": e.code, "message": e.message}), True
        except Exception as e:                      # noqa: BLE001
            log.warning("ai: tool %s failed: %s", name, type(e).__name__)
            return _json({"error": "failed", "message": "The lookup failed."}), True
        text = _json(out)
        if len(text) > MAX_TOOL_RESULT_CHARS:
            text = text[:MAX_TOOL_RESULT_CHARS] + "\n[The answer was cut here.]"
        return text, False

    @property
    def described(self) -> list[str]:
        return [c["table"] for c in self.calls if c["tool"] == "describe_table"]


# --- the prompt ---------------------------------------------------------------

_TASKS = {
    "generate": "Write the SQL for this request.",
    "explain": "Explain what this SQL does, step by step, in plain words.",
    "optimize": ("Make this SQL faster. Say why it is slow, then give the full "
                 "new SQL."),
    "fix": ("Find what is wrong with this SQL. Say what is wrong, then give the "
            "full SQL that works."),
    "chat": "",
}


def _table_list(target, database: str, cap: int) -> tuple[list[str], bool]:
    """(names, more exist). Empty when the catalog has no snapshot."""
    if cap <= 0:
        return [], True
    try:
        listing = schema_tools.table_names(target, database)
    except schema_tools.ToolError:
        return [], False
    names = [f"{t['schema']}.{t['table']}" if t.get("schema") else t["table"]
             for t in listing["tables"]]
    return names[:cap], listing["truncated"] or len(names) > cap


def system_prompt(target, database: str, s: Settings, *, tools: bool,
                  names: list[str], more: bool) -> str:
    engine = (getattr(target, "engine", None) or "postgres").strip().lower()
    label = _ENGINE_LABELS.get(engine, engine)
    lines = [
        "You are the SQL assistant of QueryHub, a web SQL editor for "
        "production databases.",
        f"The user's editor tab uses the {label} database \"{database}\" on "
        f"the connection \"{target.alias}\".",
        "",
        "Rules:",
        "- You cannot run SQL. The user reads your SQL and runs it from the "
        "editor. A DBA approves each write before it runs.",
        f"- Write SQL for {label} only.",
        "- Use only tables and columns that exist. Read the schema with your "
        "tools before you write SQL about a table." if tools else
        "- Use only tables and columns that exist. When you do not know the "
        "columns of a table, ask the user.",
        "- Put each SQL statement in a ```sql code block.",
        "- Prefer read-only SQL. Write INSERT, UPDATE, DELETE or DDL only when "
        "the user asks for it. Give each UPDATE and DELETE a WHERE clause.",
        "- QueryHub hides the values of masked columns in results. A column "
        "with \"masked\": true is one of them.",
        "- Answer in the language of the user's message. Keep the answer short.",
    ]
    if engine in _READ_ONLY_ENGINES:
        lines.append("- This connection is read-only in QueryHub. Write SELECT "
                     "statements only.")
    if s.instructions.strip():
        lines += ["", "Instructions from the QueryHub admin:", s.instructions.strip()]
    lines.append("")
    if names:
        lines.append(f"Tables of this database ({len(names)}"
                     f"{' shown, more exist' if more else ''}):")
        lines += names
        if more and tools:
            lines.append("Find the other tables with list_tables and part of a name.")
    elif tools:
        lines.append("Find the tables with list_tables.")
    else:
        lines.append("QueryHub has no table list for this database.")
    return "\n".join(lines)


@dataclass
class Ask:
    mode: str
    prompt: str = ""
    sql: str = ""
    messages: list[dict] = field(default_factory=list)
    connection: str = ""
    database: str = ""
    model: str | None = None


def user_message(ask: Ask) -> str:
    parts = [p for p in (_TASKS[ask.mode], ask.prompt.strip()) if p]
    if ask.sql.strip():
        parts.append("SQL from the editor:\n```sql\n" + ask.sql.strip() + "\n```")
    return "\n\n".join(parts)


def conversation(ask: Ask) -> list[dict]:
    """The earlier turns as plain text, then the new user message.

    Earlier turns carry text only. The first turn is always the user's. Only
    the last `MAX_TURNS` turns go out.
    """
    turns = []
    for m in (ask.messages or [])[-MAX_TURNS:]:
        role = m.get("role") if isinstance(m, dict) else None
        content = m.get("content") if isinstance(m, dict) else None
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if not content.strip():
            continue
        turns.append({"role": role, "content": content[:MAX_TURN_CHARS]})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    turns.append({"role": "user", "content": user_message(ask)})
    return turns


# --- the providers' protocols -----------------------------------------------


@dataclass
class Reply:
    text: str
    stop: str                       # end | refusal | max_tokens | rounds
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    fallback: bool = False


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 1:
        raise AIError(504, "timeout", "The AI provider did not answer in time.")
    return left


def _anthropic_tools() -> list[dict]:
    return [{"name": t["name"], "description": t["description"],
             "input_schema": t["schema"]} for t in TOOLS]


def _anthropic_client(provider: Provider, key: str, timeout: float):
    try:
        import anthropic
    except ImportError as e:                        # the optional extra
        raise AIError(503, "unavailable",
                      "The anthropic package is not installed on the server. "
                      "Install the [ai] extra.") from e
    return anthropic, anthropic.Anthropic(api_key=key, base_url=provider.base_url or None,
                                          timeout=timeout, max_retries=1)


def call_anthropic(choice: Choice, key: str, system: str, messages: list[dict],
                   toolbox: Toolbox | None, s: Settings, deadline: float) -> Reply:
    """The Anthropic Messages API, with a manual tool loop.

    Thinking is not set: it is adaptive on the current models. Effort comes
    from `ai_effort`. Forced tool choice is a 400 on the current models, so
    the choice is `auto`. Models in FALLBACK_MODELS opt into the server-side
    fallback on a refusal. Each round appends the reply unchanged, so the
    history stays append-only.
    """
    anthropic, client = _anthropic_client(choice.provider, key, _remaining(deadline))
    kwargs: dict[str, Any] = {
        "model": choice.model, "max_tokens": MAX_TOKENS, "system": system,
        "cache_control": {"type": "ephemeral"},
    }
    if toolbox is not None:
        kwargs["tools"] = _anthropic_tools()
        kwargs["tool_choice"] = {"type": "auto"}
    if s.effort:
        kwargs["output_config"] = {"effort": s.effort}
    if choice.model in FALLBACK_MODELS:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    convo: list[dict] = list(messages)
    reply = Reply(text="", stop="rounds", model=choice.model)
    for _ in range(MAX_ROUNDS):
        try:
            resp = client.beta.messages.create(messages=convo,
                                               timeout=_remaining(deadline), **kwargs)
        except anthropic.APITimeoutError as e:
            raise AIError(504, "timeout", "The AI provider did not answer in time.") from e
        except anthropic.AuthenticationError as e:
            raise AIError(502, "provider_auth", "The AI provider refused the API key. "
                                                "An admin must check the key.") from e
        except anthropic.PermissionDeniedError as e:
            raise AIError(502, "provider_auth", "The API key has no access to this "
                                                "model. An admin must check the key.") from e
        except anthropic.NotFoundError as e:
            raise AIError(502, "provider_model",
                          f"The AI provider does not know the model '{choice.model}'.") from e
        except anthropic.RateLimitError as e:
            raise AIError(503, "provider_busy", "The AI provider is at its rate limit. "
                                                "Try again in a minute.") from e
        except anthropic.BadRequestError as e:
            log.warning("ai: anthropic refused the request: HTTP 400 request_id=%s",
                        getattr(e, "request_id", None))
            raise AIError(502, "provider_rejected", "The AI provider refused the "
                                                    "request.") from e
        except anthropic.APIStatusError as e:
            raise AIError(502, "provider_error",
                          f"The AI provider returned HTTP {e.status_code}.") from e
        except anthropic.APIConnectionError as e:
            raise AIError(502, "provider_unreachable",
                          "QueryHub cannot reach the AI provider.") from e
        usage = getattr(resp, "usage", None)
        reply.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        reply.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        reply.model = getattr(resp, "model", None) or reply.model
        if any(getattr(b, "type", None) == "fallback" for b in resp.content):
            reply.fallback = True
        reply.text = "".join(b.text for b in resp.content
                             if getattr(b, "type", None) == "text")
        stop = resp.stop_reason
        if stop == "refusal":
            reply.text, reply.stop = "", "refusal"
            return reply
        if stop == "pause_turn":
            convo.append({"role": "assistant", "content": resp.content})
            continue
        uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if stop == "tool_use" and uses and toolbox is not None:
            convo.append({"role": "assistant", "content": resp.content})
            results = []
            for b in uses:
                out, failed = toolbox.run(b.name, b.input)
                item: dict[str, Any] = {"type": "tool_result", "tool_use_id": b.id,
                                        "content": out}
                if failed:
                    item["is_error"] = True
                results.append(item)
            convo.append({"role": "user", "content": results})
            continue
        reply.stop = "max_tokens" if stop == "max_tokens" else "end"
        return reply
    return reply


def _openai_tools() -> list[dict]:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["schema"]}} for t in TOOLS]


def _openai_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


def call_openai(choice: Choice, key: str, system: str, messages: list[dict],
                toolbox: Toolbox | None, s: Settings, deadline: float,
                *, transport: httpx.BaseTransport | None = None) -> Reply:
    """The OpenAI Chat Completions API, with a manual tool loop.

    The request sets no token cap and no sampling value: the names of those
    fields differ between servers that speak this API.
    """
    base = (choice.provider.base_url or "").rstrip("/")
    if not base:
        raise AIError(503, "no_base_url", "The AI provider has no base URL.")
    url = f"{base}/chat/completions"
    head = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body: dict[str, Any] = {"model": choice.model}
    if toolbox is not None:
        body["tools"] = _openai_tools()
        body["tool_choice"] = "auto"
    convo: list[dict] = [{"role": "system", "content": system}, *messages]
    reply = Reply(text="", stop="rounds", model=choice.model)
    with httpx.Client(transport=transport, follow_redirects=False) as http:
        for _ in range(MAX_ROUNDS):
            try:
                r = http.post(url, headers=head, json={**body, "messages": convo},
                              timeout=_remaining(deadline))
            except httpx.TimeoutException as e:
                raise AIError(504, "timeout", "The AI provider did not answer in time.") from e
            except httpx.HTTPError as e:
                raise AIError(502, "provider_unreachable",
                              "QueryHub cannot reach the AI provider.") from e
            if r.status_code in (401, 403):
                raise AIError(502, "provider_auth", "The AI provider refused the API key. "
                                                    "An admin must check the key.")
            if r.status_code == 404:
                raise AIError(502, "provider_model",
                              f"The AI provider does not know the model "
                              f"'{choice.model}', or the base URL is wrong.")
            if r.status_code == 429:
                raise AIError(503, "provider_busy", "The AI provider is at its rate "
                                                    "limit. Try again in a minute.")
            if r.status_code >= 400:
                log.warning("ai: %s refused the request: HTTP %s",
                            choice.provider.host, r.status_code)
                raise AIError(502, "provider_rejected",
                              f"The AI provider refused the request (HTTP {r.status_code}).")
            try:
                data = r.json()
                first = data["choices"][0]
                msg = first["message"]
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise AIError(502, "provider_error", "QueryHub cannot read the answer "
                                                     "of the AI provider.") from e
            usage = data.get("usage") or {}
            reply.input_tokens += int(usage.get("prompt_tokens") or 0)
            reply.output_tokens += int(usage.get("completion_tokens") or 0)
            reply.model = data.get("model") or reply.model
            calls = msg.get("tool_calls") or []
            if calls and toolbox is not None:
                convo.append({"role": "assistant", "content": msg.get("content"),
                              "tool_calls": calls})
                for c in calls:
                    fn = c.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = None
                    out, _failed = toolbox.run(fn.get("name"), args)
                    convo.append({"role": "tool", "tool_call_id": c.get("id"),
                                  "content": out})
                continue
            reply.text = _openai_text(msg.get("content"))
            finish = first.get("finish_reason")
            if msg.get("refusal") or finish == "content_filter":
                reply.text, reply.stop = "", "refusal"
            else:
                reply.stop = "max_tokens" if finish == "length" else "end"
            return reply
    return reply


def _call(choice: Choice, key: str, system: str, messages: list[dict],
          toolbox: Toolbox | None, s: Settings, deadline: float) -> Reply:
    if choice.provider.kind == "anthropic":
        return call_anthropic(choice, key, system, messages, toolbox, s, deadline)
    return call_openai(choice, key, system, messages, toolbox, s, deadline)


# --- one call -----------------------------------------------------------------


def _pick(s: Settings, provs: list[Provider], wanted: str | None) -> Choice:
    d = default_choice(s, provs)
    if d is None:
        raise AIError(404, "not_found", "The AI assistant is off.")
    if not wanted or not s.user_model_choice:
        return d
    for c in choices(s, provs):
        if c.id == wanted:
            return c
    raise AIError(400, "bad_request", "That model is not available. Choose one "
                                      "from the list.")


def _granted(uid: str, connection: str, database: str):
    """The target, after the grant check of the editor. Unknown, ungranted and
    disabled are one answer, as in POST /queries."""
    try:
        t, _grant = schema_tools.granted_database(uid, connection, database,
                                                  named=_named)
    except schema_tools.ToolError as e:
        if e.code == "no_access":
            raise AIError(403, "forbidden", e.message) from e
        raise AIError(404, "not_found", f"Unknown connection '{connection}'.") from e
    if not t.enabled and not admins.is_admin(uid):
        raise AIError(404, "not_found", f"Unknown connection '{connection}'.")
    # Migration 217. Refused here, before the caller reads a table name, so
    # nothing about this connection goes to the model provider.
    if not getattr(t, "ai_allowed", True):
        raise AIError(403, "forbidden", AI_OFF_FOR_CONNECTION)
    return t


#: The refusal for a connection with `ai_allowed` false (migration 217).
AI_OFF_FOR_CONNECTION = ("The AI assistant is off for this connection. "
                         "An admin can turn it on.")


def _check(ask: Ask) -> None:
    if ask.mode not in MODES:
        raise AIError(400, "bad_request", f"Unknown mode. Use one of: {', '.join(MODES)}.")
    if len(ask.prompt) > MAX_PROMPT_CHARS:
        raise AIError(400, "bad_request",
                      f"The prompt is too long. The limit is {MAX_PROMPT_CHARS} characters.")
    if len(ask.sql) > MAX_SQL_CHARS:
        raise AIError(400, "bad_request",
                      f"The SQL is too long. The limit is {MAX_SQL_CHARS} characters. "
                      f"Select a part of it.")
    if ask.mode in ("generate", "chat") and not ask.prompt.strip():
        raise AIError(400, "bad_request", "Write what you need in the prompt.")
    if ask.mode in ("explain", "optimize", "fix") and not ask.sql.strip():
        raise AIError(400, "bad_request", "There is no SQL. Write or select SQL first.")
    if not ask.connection or not ask.database:
        raise AIError(400, "bad_request", "Choose a connection and a database first.")


def complete(uid: str, actor_name: str | None, ask: Ask) -> dict:
    """One assistant call: checks, the provider round trip, one audit row."""
    s = settings()
    provs = providers()
    if not s.enabled:
        raise AIError(404, "not_found", "The AI assistant is off.")
    choice = _pick(s, provs, ask.model)
    if not in_audience(uid):
        raise AIError(403, "forbidden", "The AI assistant is not open for you. "
                                        "Ask a super-admin.")
    _check(ask)
    t = _granted(uid, ask.connection, ask.database)
    if not take_slot(uid, s.max_calls_per_hour):
        raise AIError(429, "rate_limited",
                      f"You made {s.max_calls_per_hour} assistant requests in the "
                      f"last hour. Try again later.")

    key = _key_of(choice.provider.id)
    started = time.monotonic()
    deadline = started + s.timeout_sec
    tools = choice.provider.supports_tools
    names, more = _table_list(t, ask.database, s.schema_max_tables)
    system = system_prompt(t, ask.database, s, tools=tools, names=names, more=more)
    messages = conversation(ask)
    toolbox = Toolbox(uid, actor_name, t, ask.database) if tools else None

    outcome, reply = "ok", None
    try:
        reply = _call(choice, key, system, messages, toolbox, s, deadline)
        if reply.stop == "refusal":
            outcome = "refused"
        return _answer(choice, reply, ask, names, toolbox, started)
    except AIError as e:
        outcome = e.code
        raise
    except Exception as e:                          # noqa: BLE001
        outcome = "error"
        log.exception("ai: call failed: %s", type(e).__name__)
        raise AIError(502, "provider_error", "The AI request failed.") from e
    finally:
        audit.log(None, uid, actor_name, "ai_call", {
            "mode": ask.mode, "provider": choice.provider.name,
            "host": choice.provider.host, "model": choice.model,
            "served_by": reply.model if reply else None,
            "fallback": bool(reply and reply.fallback),
            "target_id": t.id, "database": ask.database,
            "prompt_chars": len(ask.prompt), "sql_chars": len(ask.sql),
            "turns": len(messages), "table_names_sent": len(names),
            "tools": [c["tool"] for c in toolbox.calls] if toolbox else [],
            "tables_read": toolbox.described if toolbox else [],
            "response_chars": len(reply.text) if reply else 0,
            "input_tokens": reply.input_tokens if reply else 0,
            "output_tokens": reply.output_tokens if reply else 0,
            "status": outcome,
            "ms": int((time.monotonic() - started) * 1000),
        })


def _answer(choice: Choice, reply: Reply, ask: Ask, names: list[str],
            toolbox: Toolbox | None, started: float) -> dict:
    out = {
        "text": reply.text,
        "model": choice.id,
        "provider": choice.provider.name,
        "modelName": reply.model,
        "stop": reply.stop,
        "refused": reply.stop == "refusal",
        "sent": {
            "promptChars": len(ask.prompt),
            "sqlChars": len(ask.sql),
            "tableNames": len(names),
            "tablesRead": toolbox.described if toolbox else [],
        },
        "ms": int((time.monotonic() - started) * 1000),
    }
    if reply.stop == "refusal":
        out["note"] = "The AI provider declined this request."
    elif reply.stop == "max_tokens":
        out["note"] = "The answer reached the length limit and stops early."
    elif reply.stop == "rounds":
        out["note"] = "The assistant used all its steps. The answer can be incomplete."
    return out


def test_provider(uid: str, actor_name: str | None, provider_id: int,
                  model: str | None = None) -> dict:
    """A small call with no tools and no schema, from the admin form."""
    p = next((p for p in providers() if p.id == provider_id), None)
    if p is None:
        raise AIError(404, "not_found", "Unknown provider.")
    if not p.models:
        raise AIError(400, "bad_request", "Add a model to the provider first.")
    if model and model not in p.models:
        raise AIError(400, "bad_request", "That model is not in the list of the provider.")
    choice = Choice(p, model or p.models[0])
    key = _key_of(p.id)
    s = settings()
    started = time.monotonic()
    outcome, reply = "ok", None
    try:
        reply = _call(choice, key, "Reply with the single word OK.",
                      [{"role": "user", "content": "Say OK."}], None, s,
                      started + min(s.timeout_sec, 60))
        if reply.stop == "refusal":
            outcome = "refused"
        return {"ok": reply.stop != "refusal", "model": reply.model,
                "text": reply.text[:200],
                "ms": int((time.monotonic() - started) * 1000)}
    except AIError as e:
        outcome = e.code
        return {"ok": False, "code": e.code, "message": e.message,
                "ms": int((time.monotonic() - started) * 1000)}
    finally:
        audit.log(None, uid, actor_name, "ai_provider_tested", {
            "provider": p.name, "host": p.host, "model": choice.model,
            "status": outcome})


# --- admin writes -------------------------------------------------------------


def clean_base_url(kind: str, raw: str | None) -> str | None:
    """The base URL, checked. https only, except for a loopback host."""
    url = (raw or "").strip()
    if not url:
        if kind == "openai":
            raise ValueError("An OpenAI-compatible provider needs a base URL, for "
                             "example https://api.openai.com/v1.")
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("The base URL must start with https://.")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise ValueError("Use https:// for the base URL. QueryHub accepts plain "
                         "http:// only for localhost.")
    if parts.username or parts.password:
        raise ValueError("Do not put a user name or a password in the base URL. "
                         "Use the API key field.")
    if parts.query or parts.fragment:
        raise ValueError("The base URL cannot have a query string.")
    return url.rstrip("/")


def clean_models(raw: Any) -> list[str]:
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(items, list):
        raise ValueError("Models must be a list of model names.")
    out: list[str] = []
    for item in items:
        name = str(item).strip()
        if not name:
            continue
        if len(name) > MAX_MODEL_CHARS or not _MODEL_RE.match(name):
            raise ValueError(f"'{name[:60]}' is not a model name.")
        if name not in out:
            out.append(name)
    if len(out) > MAX_MODELS:
        raise ValueError(f"A provider can have at most {MAX_MODELS} models.")
    return out


def clean_provider(body: dict, current: Provider | None = None) -> dict:
    """The provider fields of an admin write, checked. A field that is not in
    `body` keeps its current value."""
    def pick(key: str, now: Any) -> Any:
        return body[key] if key in body else now

    name = str(pick("name", current.name if current else "") or "").strip()
    if not name or len(name) > MAX_NAME_CHARS:
        raise ValueError(f"The name must have 1 to {MAX_NAME_CHARS} characters.")
    kind = pick("kind", current.kind if current else "")
    if kind not in KINDS:
        raise ValueError("The kind must be 'anthropic' or 'openai'.")
    base_url = clean_base_url(kind, pick("baseUrl", current.base_url if current else None))
    models = clean_models(pick("models", list(current.models) if current else []))
    return {
        "name": name, "kind": kind, "base_url": base_url, "models": models,
        "supports_tools": bool(pick("supportsTools",
                                    current.supports_tools if current else True)),
        "enabled": bool(pick("enabled", current.enabled if current else True)),
    }


def clean_key(raw: Any) -> str:
    key = str(raw or "").strip()
    if not key or len(key) > MAX_KEY_CHARS or any(ch.isspace() for ch in key):
        raise ValueError("The API key is not valid.")
    return key


def get_provider(provider_id: int) -> Provider | None:
    row = db.fetch_one(f"SELECT {_PROVIDER_COLS} FROM ai_providers WHERE id = %s",
                       (provider_id,))
    return _provider(row) if row else None


def create_provider(cur, fields: dict, api_key: str | None, uid: str,
                    actor_name: str | None) -> int:
    cipher = crypto.encrypt(api_key) if api_key else None
    cur.execute(
        "INSERT INTO ai_providers (name, kind, base_url, models, supports_tools, "
        "  enabled, api_key_encrypted, key_set_by, key_set_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END) "
        "RETURNING id",
        (fields["name"], fields["kind"], fields["base_url"], fields["models"],
         fields["supports_tools"], fields["enabled"], cipher,
         uid if cipher else None, cipher is not None))
    pid = cur.fetchone()["id"]
    audit.log_in(cur, None, uid, actor_name, "ai_provider_created", {
        "provider_id": pid, "name": fields["name"], "kind": fields["kind"],
        "host": urlsplit(fields["base_url"]).hostname if fields["base_url"] else None,
        "models": fields["models"], "key": "set" if cipher else "none"})
    return pid


def update_provider(cur, current: Provider, fields: dict, api_key: str | None,
                    clear_key: bool, uid: str, actor_name: str | None) -> None:
    changed = [k for k, now in (("name", current.name), ("kind", current.kind),
                                ("base_url", current.base_url),
                                ("models", list(current.models)),
                                ("supports_tools", current.supports_tools),
                                ("enabled", current.enabled))
               if fields[k] != now]
    cur.execute(
        "UPDATE ai_providers SET name = %s, kind = %s, base_url = %s, models = %s, "
        "  supports_tools = %s, enabled = %s, updated_at = now() WHERE id = %s",
        (fields["name"], fields["kind"], fields["base_url"], fields["models"],
         fields["supports_tools"], fields["enabled"], current.id))
    key_change = "unchanged"
    if api_key:
        cur.execute(
            "UPDATE ai_providers SET api_key_encrypted = %s, key_set_by = %s, "
            "  key_set_at = now() WHERE id = %s",
            (crypto.encrypt(api_key), uid, current.id))
        key_change = "set"
    elif clear_key:
        cur.execute(
            "UPDATE ai_providers SET api_key_encrypted = NULL, key_set_by = NULL, "
            "  key_set_at = NULL WHERE id = %s", (current.id,))
        key_change = "cleared"
    audit.log_in(cur, None, uid, actor_name, "ai_provider_updated", {
        "provider_id": current.id, "name": fields["name"], "changed": changed,
        "key": key_change})


def delete_provider(cur, current: Provider, uid: str, actor_name: str | None) -> None:
    cur.execute("DELETE FROM ai_providers WHERE id = %s", (current.id,))
    audit.log_in(cur, None, uid, actor_name, "ai_provider_deleted", {
        "provider_id": current.id, "name": current.name, "kind": current.kind})


# The admin form's field -> (bot_config key, kind). A tuple kind is an int
# range.
SETTING_FIELDS: dict[str, tuple[str, str | tuple[int, int]]] = {
    "enabled": ("ai_enabled", "bool"),
    "userModelChoice": ("ai_user_model_choice", "bool"),
    "defaultProvider": ("ai_default_provider", "provider"),
    "defaultModel": ("ai_default_model", "model"),
    "effort": ("ai_effort", "effort"),
    "instructions": ("ai_instructions", "text"),
    "maxCallsPerHour": ("ai_max_calls_per_hour", (1, 1000)),
    "timeoutSec": ("ai_timeout_sec", (10, 600)),
    "schemaMaxTables": ("ai_schema_max_tables", (0, schema_tools.MAX_TABLES)),
}


def settings_view(s: Settings) -> dict:
    return {
        "enabled": s.enabled, "userModelChoice": s.user_model_choice,
        "defaultProvider": s.default_provider, "defaultModel": s.default_model,
        "effort": s.effort, "instructions": s.instructions,
        "maxCallsPerHour": s.max_calls_per_hour, "timeoutSec": s.timeout_sec,
        "schemaMaxTables": s.schema_max_tables,
    }


def clean_settings(changes: dict, provs: list[Provider], s: Settings) -> dict[str, str]:
    """bot_config key -> new text value, for the fields in `changes`."""
    out: dict[str, str] = {}
    view = settings_view(s)
    for name, raw in (changes or {}).items():
        if name not in SETTING_FIELDS:
            raise ValueError(f"Unknown setting '{name}'.")
        key, kind = SETTING_FIELDS[name]
        if kind == "bool":
            if not isinstance(raw, bool):
                raise ValueError(f"{name} must be true or false.")
            out[key] = "on" if raw else "off"
        elif kind == "provider":
            if raw in (None, ""):
                out[key] = ""
            elif isinstance(raw, int) and any(p.id == raw for p in provs):
                out[key] = str(raw)
            else:
                raise ValueError("The default provider does not exist.")
        elif kind == "model":
            out[key] = str(raw or "").strip()
        elif kind == "effort":
            value = str(raw or "").strip().lower()
            if value not in EFFORTS:
                raise ValueError("The effort must be empty, low, medium, high, "
                                 "xhigh or max.")
            out[key] = value
        elif kind == "text":
            value = str(raw or "")
            if len(value) > MAX_INSTRUCTIONS_CHARS:
                raise ValueError(f"The instructions are too long. The limit is "
                                 f"{MAX_INSTRUCTIONS_CHARS} characters.")
            out[key] = value
        elif isinstance(kind, tuple):
            lo, hi = kind
            if not isinstance(raw, int) or isinstance(raw, bool) or not lo <= raw <= hi:
                raise ValueError(f"{name} must be a whole number from {lo} to {hi}.")
            out[key] = str(raw)
        view[name] = raw
    # The default model must belong to the default provider. A new provider
    # with no model named in the same save starts on its first model.
    pid = view["defaultProvider"] if "defaultProvider" in (changes or {}) else s.default_provider
    if "defaultProvider" in (changes or {}) and "defaultModel" not in (changes or {}):
        new = next((p for p in provs if p.id == pid), None)
        if s.default_model and (new is None or s.default_model not in new.models):
            out["ai_default_model"] = ""
    model = out.get("ai_default_model", s.default_model)
    if model:
        p = next((p for p in provs if p.id == pid), None)
        if p is None or model not in p.models:
            raise ValueError("The default model must be one of the models of the "
                             "default provider.")
    return out


def save_settings(cur, values: dict[str, str], uid: str, actor_name: str | None) -> list[str]:
    """Write the changed keys and one audit row. Returns the changed keys."""
    cur.execute("SELECT key, value FROM bot_config WHERE key = ANY(%s)", (list(values),))
    current = {r["key"]: r["value"] for r in cur.fetchall()}
    changed = []
    for key, value in values.items():
        if current.get(key) == value:
            continue
        cur.execute(
            "INSERT INTO bot_config (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            (key, value))
        changed.append(key)
    if changed:
        audit.log_in(cur, None, uid, actor_name, "ai_settings_set", {
            "changes": [{"key": k,
                         "to": (f"<{len(values[k])} characters>"
                                if k == "ai_instructions" else values[k])}
                        for k in changed]})
    return changed


def provider_view(p: Provider) -> dict:
    """A provider for the admin form. It has no key, only who set it."""
    return {
        "id": p.id, "name": p.name, "kind": p.kind, "baseUrl": p.base_url,
        "models": list(p.models), "supportsTools": p.supports_tools,
        "enabled": p.enabled, "keySet": p.key_set, "keySetBy": p.key_set_by,
        "keySetAt": p.key_set_at.isoformat() if p.key_set_at else None,
    }
