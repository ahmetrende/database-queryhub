"""The AI assistant of the web editor (M13, PR B): settings, providers, the two
protocols, the tools of the model, one call, and the audit row.

What these tests pin:

- off means off: no provider call, `status` says only `enabled: false`;
- the grant check runs before the provider call, and the tools stay on the
  connection and the database of the tab;
- the Anthropic request has the shape that the current models accept
  (no `thinking`, `tool_choice` auto, effort, server-side fallbacks);
- the OpenAI-compatible request is a real HTTP round trip with a tool loop;
- the API key never comes back, and the audit row holds no prompt, no SQL and
  no answer.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

# The `anthropic` SDK is the optional [ai] extra. The base install has no SDK,
# so only the tests that build an SDK error need it. The optional-extras CI job
# installs the extra and runs them.
try:
    import anthropic
    import httpx2
except ImportError:                                 # the base install
    anthropic = httpx2 = None
needs_sdk = pytest.mark.skipif(anthropic is None, reason="the [ai] extra is not installed")

from queryhub import ai, audit, schema_tools

ME = "U0EXAMPLE001"
PG = SimpleNamespace(id=7, alias="srv-alpha", engine="postgres", host="h", port=5432,
                     enabled=True)


def _settings(**kw) -> ai.Settings:
    base = dict(enabled=True, default_provider=1, default_model="", user_model_choice=False,
                effort="", instructions="", max_calls_per_hour=30, timeout_sec=60,
                schema_max_tables=200)
    base.update(kw)
    return ai.Settings(**base)


def _prov(pid=1, name="Anthropic", kind="anthropic", base_url=None,
          models=("claude-opus-5-5",), tools=True, enabled=True, key=True) -> ai.Provider:
    return ai.Provider(id=pid, name=name, kind=kind, base_url=base_url, models=tuple(models),
                       supports_tools=tools, enabled=enabled, key_set=key)


@pytest.fixture(autouse=True)
def _fresh_limit():
    ai.reset_rate_limit()
    yield
    ai.reset_rate_limit()


# --- settings and choices ---------------------------------------------------------


def test_settings_read_the_keys_and_clamp_them(monkeypatch):
    vals = {"ai_enabled": "on", "ai_default_provider": "3", "ai_default_model": " m1 ",
            "ai_user_model_choice": "off", "ai_effort": "HIGH", "ai_instructions": "x" * 9000,
            "ai_max_calls_per_hour": "0", "ai_timeout_sec": "9999",
            "ai_schema_max_tables": "abc"}
    monkeypatch.setattr(ai.cfg, "get_setting", lambda k, d=None: vals.get(k, d))
    s = ai.settings()
    assert s.enabled and s.default_provider == 3 and s.default_model == "m1"
    assert s.effort == "high" and len(s.instructions) == ai.MAX_INSTRUCTIONS_CHARS
    assert (s.max_calls_per_hour, s.timeout_sec, s.schema_max_tables) == (1, 600, 200)


def test_an_unknown_effort_reads_as_the_model_default(monkeypatch):
    monkeypatch.setattr(ai.cfg, "get_setting",
                        lambda k, d=None: "turbo" if k == "ai_effort" else d)
    assert ai.settings().effort == ""


def test_the_default_needs_a_usable_provider():
    s = _settings(default_model="claude-sonnet-5-5")
    p = _prov(models=("claude-opus-5-5", "claude-sonnet-5-5"))
    assert ai.default_choice(s, [p]).model == "claude-sonnet-5-5"
    # A default model outside the list falls back to the first model.
    assert ai.default_choice(_settings(default_model="x"), [p]).model == "claude-opus-5-5"
    for bad in (_prov(key=False), _prov(enabled=False), _prov(models=())):
        assert ai.default_choice(s, [bad]) is None
    assert ai.default_choice(_settings(default_provider=None), [p]) is None


def test_users_see_only_the_default_unless_the_admin_lets_them_choose():
    a = _prov(models=("claude-opus-5-5", "claude-haiku-5-5"))
    b = _prov(pid=2, name="DeepSeek", kind="openai", base_url="https://x.test",
              models=("deepseek-chat",))
    off = _prov(pid=3, name="Off", kind="openai", base_url="https://y.test",
                models=("m",), enabled=False)
    assert [c.id for c in ai.choices(_settings(), [a, b, off])] == ["1:claude-opus-5-5"]
    ids = [c.id for c in ai.choices(_settings(user_model_choice=True,
                                              default_provider=2), [a, b, off])]
    assert ids[0] == "2:deepseek-chat"
    assert set(ids) == {"2:deepseek-chat", "1:claude-opus-5-5", "1:claude-haiku-5-5"}


@pytest.mark.parametrize("s,provs,why", [
    (_settings(enabled=False), [_prov()], "The assistant is off."),
    (_settings(default_provider=None), [_prov()], "Choose a default provider."),
    (_settings(default_provider=9), [_prov()], "The default provider does not exist."),
    (_settings(), [_prov(enabled=False)], "The default provider is off."),
    (_settings(), [_prov(key=False)], "The default provider has no API key."),
    (_settings(), [_prov(models=())], "The default provider has no model."),
    (_settings(), [_prov()], None),
])
def test_readiness_names_the_missing_piece(s, provs, why):
    assert ai.readiness(s, provs) == (why is None, why)


def test_status_says_nothing_but_off_when_off(monkeypatch):
    monkeypatch.setattr(ai, "in_audience", lambda uid, rules=None: True)
    monkeypatch.setattr(ai, "settings", lambda: _settings(enabled=False))
    monkeypatch.setattr(ai, "providers", lambda: [_prov()])
    assert ai.status(ME) == {"enabled": False}
    monkeypatch.setattr(ai, "settings", lambda: _settings())
    monkeypatch.setattr(ai, "providers", lambda: [_prov(key=False)])
    assert ai.status(ME) == {"enabled": False}


def test_status_when_on_lists_the_models_and_what_leaves(monkeypatch):
    monkeypatch.setattr(ai, "in_audience", lambda uid, rules=None: True)
    monkeypatch.setattr(ai, "settings", lambda: _settings(user_model_choice=True))
    monkeypatch.setattr(ai, "providers",
                        lambda: [_prov(models=("claude-opus-5-5", "claude-haiku-5-5"))])
    st = ai.status(ME)
    assert st["enabled"] and st["default"] == "1:claude-opus-5-5"
    assert st["userModelChoice"] is True and len(st["models"]) == 2
    assert st["sends"] == ["prompt", "sql", "tableNames", "tableDetail"]
    assert "key" not in json.dumps(st).lower()


def test_the_rate_limit_counts_one_hour():
    assert all(ai.take_slot(ME, 2, now=t) for t in (0.0, 1.0))
    assert not ai.take_slot(ME, 2, now=2.0)
    assert ai.take_slot("U0OTHER00001", 2, now=2.0)
    assert ai.take_slot(ME, 2, now=3600.5)


# --- the prompt ---------------------------------------------------------------------


def test_the_user_message_has_the_task_the_prompt_and_the_sql():
    ask = ai.Ask(mode="fix", prompt="It says syntax error", sql="SELEC 1")
    text = ai.user_message(ask)
    assert text.startswith("Find what is wrong with this SQL.")
    assert "It says syntax error" in text and "```sql\nSELEC 1\n```" in text
    assert ai.user_message(ai.Ask(mode="chat", prompt="hi")) == "hi"


def test_earlier_turns_go_as_text_and_start_with_the_user():
    ask = ai.Ask(mode="chat", prompt="and now?", messages=[
        {"role": "assistant", "content": "stray"},
        {"role": "user", "content": "first"},
        {"role": "system", "content": "no"},
        {"role": "assistant", "content": ["not text"]},
        {"role": "assistant", "content": "x" * (ai.MAX_TURN_CHARS + 5)},
        {"role": "user", "content": "   "},
    ])
    turns = ai.conversation(ask)
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[0]["content"] == "first"
    assert len(turns[1]["content"]) == ai.MAX_TURN_CHARS
    assert turns[-1]["content"] == "and now?"


def test_only_the_last_turns_go_out():
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"t{i}"}
            for i in range(60)]
    turns = ai.conversation(ai.Ask(mode="chat", prompt="p", messages=msgs))
    assert len(turns) <= ai.MAX_TURNS + 1 and turns[0]["role"] == "user"


def test_the_system_prompt_names_the_engine_the_database_and_the_tables():
    s = _settings(instructions="Use CTEs.")
    text = ai.system_prompt(PG, "shop", s, tools=True, names=["public.orders"], more=True)
    assert 'PostgreSQL database "shop"' in text and '"srv-alpha"' in text
    assert "public.orders" in text and "more exist" in text and "list_tables" in text
    assert "You cannot run SQL." in text and "Use CTEs." in text
    ch = SimpleNamespace(**{**PG.__dict__, "engine": "clickhouse"})
    assert "read-only in QueryHub" in ai.system_prompt(ch, "d", _settings(), tools=False,
                                                       names=[], more=False)


def test_a_provider_with_no_tools_is_told_to_ask():
    text = ai.system_prompt(PG, "shop", _settings(), tools=False, names=[], more=False)
    assert "ask the user" in text and "list_tables" not in text


# --- the tools of the model ---------------------------------------------------------


@pytest.fixture
def lookups(monkeypatch):
    seen = []

    def names(t, database):
        seen.append(("names", t.alias, database))
        return {"tables": [{"schema": "public", "table": "orders"}], "truncated": False}

    def matching(t, database, table, named):
        seen.append(("match", database, table, named("describe_database")))
        return {"tables": []}

    def detail(uid, actor, t, database, table, via, named):
        seen.append(("detail", uid, database, table, via))
        return {"table": table, "createScript": "CREATE TABLE orders ()"}

    monkeypatch.setattr(schema_tools, "table_names", names)
    monkeypatch.setattr(schema_tools, "tables_matching", matching)
    monkeypatch.setattr(schema_tools, "table_detail", detail)
    return seen


def test_the_tools_stay_on_the_tab_database(lookups):
    box = ai.Toolbox(ME, "Ex", PG, "shop")
    assert json.loads(box.run("list_tables", {})[0])["tables"][0]["table"] == "orders"
    box.run("list_tables", {"table": "ord", "database": "other"})
    out, failed = box.run("describe_table", {"table": "public.orders"})
    assert not failed and "CREATE TABLE" in out
    assert lookups == [("names", "srv-alpha", "shop"),
                       ("match", "shop", "ord", "`list_tables`"),
                       ("detail", ME, "shop", "public.orders", "ai")]
    assert box.described == ["public.orders"]


def test_a_tool_refusal_goes_back_as_an_error(lookups, monkeypatch):
    def refuse(*a, **k):
        raise schema_tools.ToolError("no_match", "No table named x.")
    monkeypatch.setattr(schema_tools, "table_detail", refuse)
    out, failed = ai.Toolbox(ME, "Ex", PG, "shop").run("describe_table", {"table": "x"})
    assert failed and json.loads(out) == {"error": "no_match", "message": "No table named x."}


def test_a_tool_crash_says_nothing_of_the_cause(lookups, monkeypatch):
    def crash(*a, **k):
        raise RuntimeError("password=hunter2 host=db.internal")
    monkeypatch.setattr(schema_tools, "table_detail", crash)
    out, failed = ai.Toolbox(ME, "Ex", PG, "shop").run("describe_table", {"table": "x"})
    assert failed and "hunter2" not in out and "db.internal" not in out


@pytest.mark.parametrize("name,args,code", [
    ("submit_query", {"sql": "DROP TABLE t"}, "unknown_tool"),
    ("describe_table", None, "bad_arguments"),
    ("describe_table", "orders", "bad_arguments"),
])
def test_only_the_two_schema_tools_exist(lookups, name, args, code):
    out, failed = ai.Toolbox(ME, "Ex", PG, "shop").run(name, args)
    assert failed and json.loads(out)["error"] == code


def test_the_lookups_of_one_call_have_a_cap(lookups):
    box = ai.Toolbox(ME, "Ex", PG, "shop")
    for _ in range(ai.MAX_TOOL_CALLS):
        assert not box.run("list_tables", {})[1]
    out, failed = box.run("list_tables", {})
    assert failed and json.loads(out)["error"] == "limit"


def test_a_long_tool_answer_is_cut(lookups, monkeypatch):
    monkeypatch.setattr(schema_tools, "table_detail",
                        lambda *a, **k: {"createScript": "x" * (ai.MAX_TOOL_RESULT_CHARS * 2)})
    out, _ = ai.Toolbox(ME, "Ex", PG, "shop").run("describe_table", {"table": "t"})
    assert len(out) < ai.MAX_TOOL_RESULT_CHARS + 100 and out.endswith("cut here.]")


# --- the Anthropic protocol -----------------------------------------------------------


def _block(kind, **kw):
    return SimpleNamespace(type=kind, **kw)


def _resp(content, stop, model="claude-opus-5-5", tin=10, tout=5):
    return SimpleNamespace(content=content, stop_reason=stop, model=model,
                           usage=SimpleNamespace(input_tokens=tin, output_tokens=tout))


class FakeAnthropic:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_anthropic(monkeypatch):
    holder = {}

    def make(replies):
        client = FakeAnthropic(replies)
        holder["client"] = client
        monkeypatch.setattr(ai, "_anthropic_client",
                            lambda provider, key, timeout: (anthropic, client))
        return client
    return make


def _deadline():
    return time.monotonic() + 60


def test_the_anthropic_request_has_the_shape_of_the_current_models(fake_anthropic, lookups):
    client = fake_anthropic([_resp([_block("text", text="SELECT 1;")], "end_turn")])
    choice = ai.Choice(_prov(), "claude-opus-5-5")
    box = ai.Toolbox(ME, "Ex", PG, "shop")
    reply = ai.call_anthropic(choice, "k", "SYS", [{"role": "user", "content": "q"}],
                              box, _settings(effort="high"), _deadline())
    kw = client.calls[0]
    assert "thinking" not in kw
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == ai.MAX_TOKENS
    assert kw["tool_choice"] == {"type": "auto"} and kw["system"] == "SYS"
    assert {t["name"] for t in kw["tools"]} == {"list_tables", "describe_table"}
    assert kw["output_config"] == {"effort": "high"}
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert (reply.text, reply.stop, reply.input_tokens) == ("SELECT 1;", "end", 10)


def test_no_fallback_or_effort_where_they_do_not_apply(fake_anthropic):
    client = fake_anthropic([_resp([_block("text", text="ok")], "end_turn")])
    choice = ai.Choice(_prov(models=("claude-haiku-5-5",)), "claude-haiku-5-5")
    ai.call_anthropic(choice, "k", "S", [{"role": "user", "content": "q"}], None,
                      _settings(), _deadline())
    kw = client.calls[0]
    assert not {"betas", "fallbacks", "output_config", "tools", "tool_choice"} & set(kw)


def test_the_tool_loop_appends_the_reply_unchanged(fake_anthropic, lookups):
    first = [_block("text", text="Let me look."),
             _block("tool_use", id="tu_1", name="describe_table", input={"table": "orders"}),
             _block("tool_use", id="tu_2", name="nope", input={})]
    client = fake_anthropic([_resp(first, "tool_use"),
                             _resp([_block("text", text="SELECT id FROM orders;")], "end_turn",
                                   tin=20, tout=7)])
    reply = ai.call_anthropic(ai.Choice(_prov(), "claude-opus-5-5"), "k", "S",
                              [{"role": "user", "content": "q"}],
                              ai.Toolbox(ME, "Ex", PG, "shop"), _settings(), _deadline())
    second = client.calls[1]["messages"]
    assert second[1] == {"role": "assistant", "content": first}
    results = second[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]
    assert "is_error" not in results[0] and results[1]["is_error"] is True
    assert reply.text == "SELECT id FROM orders;"
    assert (reply.input_tokens, reply.output_tokens) == (30, 12)


def test_a_refusal_returns_no_text(fake_anthropic):
    fake_anthropic([_resp([_block("text", text="partial")], "refusal")])
    reply = ai.call_anthropic(ai.Choice(_prov(), "claude-opus-5-5"), "k", "S",
                              [{"role": "user", "content": "q"}], None, _settings(),
                              _deadline())
    assert (reply.text, reply.stop) == ("", "refusal")


def test_a_fallback_and_a_pause_are_followed(fake_anthropic):
    client = fake_anthropic([
        _resp([_block("fallback")], "pause_turn"),
        _resp([_block("text", text="done")], "max_tokens", model="claude-opus-4-8")])
    reply = ai.call_anthropic(ai.Choice(_prov(), "claude-opus-5-5"), "k", "S",
                              [{"role": "user", "content": "q"}], None, _settings(),
                              _deadline())
    assert len(client.calls) == 2 and reply.fallback is True
    assert (reply.stop, reply.model) == ("max_tokens", "claude-opus-4-8")


def _sdk_error(name, status):
    """An SDK exception, built in the test so that collection needs no SDK."""
    cls = getattr(anthropic, name)
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    if status is None:
        return cls(request=req)
    return cls("x", response=httpx2.Response(status, request=req), body=None)


@needs_sdk
@pytest.mark.parametrize("name,status,code", [
    ("AuthenticationError", 401, "provider_auth"),
    ("NotFoundError", 404, "provider_model"),
    ("RateLimitError", 429, "provider_busy"),
    ("BadRequestError", 400, "provider_rejected"),
    ("InternalServerError", 500, "provider_error"),
    ("APIConnectionError", None, "provider_unreachable"),
    ("APITimeoutError", None, "timeout"),
])
def test_anthropic_errors_become_short_answers(fake_anthropic, name, status, code):
    fake_anthropic([_sdk_error(name, status)])
    with pytest.raises(ai.AIError) as got:
        ai.call_anthropic(ai.Choice(_prov(), "claude-opus-5-5"), "k", "S",
                          [{"role": "user", "content": "q"}], None, _settings(), _deadline())
    assert got.value.code == code


# --- the OpenAI-compatible protocol, over a real socket ------------------------------------


class _Server:
    """A local Chat Completions server. Each POST takes the next answer."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):                     # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"path": self.path,
                                       "auth": self.headers.get("Authorization"),
                                       "body": json.loads(self.rfile.read(n))})
                status, payload = outer.answers.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _openai_answer(message, finish="stop"):
    return {"model": "deepseek-chat", "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4}}


def test_the_openai_round_trip_runs_the_tool_loop(lookups):
    call = {"id": "call_1", "type": "function",
            "function": {"name": "describe_table", "arguments": '{"table": "orders"}'}}
    srv = _Server([
        (200, _openai_answer({"role": "assistant", "content": None, "tool_calls": [call]},
                             "tool_calls")),
        (200, _openai_answer({"role": "assistant", "content": "SELECT id FROM orders;"})),
    ])
    try:
        p = _prov(pid=2, name="DeepSeek", kind="openai", base_url=srv.url,
                  models=("deepseek-chat",))
        reply = ai.call_openai(ai.Choice(p, "deepseek-chat"), "sk-test", "SYS",
                               [{"role": "user", "content": "q"}],
                               ai.Toolbox(ME, "Ex", PG, "shop"), _settings(), _deadline())
    finally:
        srv.close()
    first, second = srv.requests
    assert first["path"] == "/v1/chat/completions" and first["auth"] == "Bearer sk-test"
    assert first["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert first["body"]["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in first["body"]["tools"]} == {
        "list_tables", "describe_table"}
    assert "max_tokens" not in first["body"] and "temperature" not in first["body"]
    tail = second["body"]["messages"][-2:]
    assert tail[0]["tool_calls"] == [call]
    assert tail[1]["role"] == "tool" and tail[1]["tool_call_id"] == "call_1"
    assert "CREATE TABLE" in tail[1]["content"]
    assert (reply.text, reply.stop, reply.input_tokens) == ("SELECT id FROM orders;", "end", 22)


def _mock(status, payload=None, raw=None):
    def handler(request):
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("transport,code", [
    (_mock(401, {}), "provider_auth"),
    (_mock(404, {}), "provider_model"),
    (_mock(429, {}), "provider_busy"),
    (_mock(500, {}), "provider_rejected"),
    (_mock(200, raw=b"not json"), "provider_error"),
    (_mock(200, {"choices": []}), "provider_error"),
])
def test_openai_errors_become_short_answers(transport, code):
    p = _prov(pid=2, kind="openai", base_url="https://x.test/v1", models=("m",))
    with pytest.raises(ai.AIError) as got:
        ai.call_openai(ai.Choice(p, "m"), "k", "S", [{"role": "user", "content": "q"}],
                       None, _settings(), _deadline(), transport=transport)
    assert got.value.code == code


def test_an_openai_refusal_and_a_cut_answer():
    p = _prov(pid=2, kind="openai", base_url="https://x.test/v1", models=("m",))
    refused = _mock(200, _openai_answer({"content": None, "refusal": "no"}))
    cut = _mock(200, _openai_answer({"content": [{"type": "text", "text": "SEL"}]}, "length"))
    args = (ai.Choice(p, "m"), "k", "S", [{"role": "user", "content": "q"}], None, _settings())
    assert ai.call_openai(*args, _deadline(), transport=refused).stop == "refusal"
    reply = ai.call_openai(*args, _deadline(), transport=cut)
    assert (reply.text, reply.stop) == ("SEL", "max_tokens")


def test_a_spent_deadline_is_a_timeout():
    p = _prov(pid=2, kind="openai", base_url="https://x.test/v1", models=("m",))
    with pytest.raises(ai.AIError) as got:
        ai.call_openai(ai.Choice(p, "m"), "k", "S", [], None, _settings(),
                       time.monotonic(), transport=_mock(200, {}))
    assert got.value.code == "timeout"


# --- one call -----------------------------------------------------------------------


@pytest.fixture
def call(monkeypatch, lookups):
    """`ai.complete` with the bot DB and the provider stubbed. `st` holds what
    happened."""
    st = {"settings": _settings(), "providers": [_prov()], "audit": [], "calls": [],
          "grant": None, "admin": False,
          "reply": ai.Reply(text="SELECT 1;", stop="end", model="claude-opus-5-5",
                            input_tokens=3, output_tokens=2)}
    monkeypatch.setattr(ai, "settings", lambda: st["settings"])
    monkeypatch.setattr(ai, "providers", lambda: st["providers"])
    monkeypatch.setattr(ai, "_key_of", lambda pid: "sk-SECRET-KEY")
    monkeypatch.setattr(ai.admins, "is_admin", lambda uid: st["admin"])
    st["audience"] = True
    monkeypatch.setattr(ai, "in_audience", lambda uid, rules=None: st["audience"])

    def granted(uid, connection, database, named):
        if st["grant"]:
            raise schema_tools.ToolError(st["grant"], "You have no access to that database.")
        return PG, {}
    monkeypatch.setattr(schema_tools, "granted_database", granted)

    def fake_call(choice, key, system, messages, toolbox, s, deadline):
        st["calls"].append({"choice": choice, "key": key, "system": system,
                            "messages": messages, "toolbox": toolbox})
        if isinstance(st["reply"], Exception):
            raise st["reply"]
        return st["reply"]
    monkeypatch.setattr(ai, "_call", fake_call)
    monkeypatch.setattr(audit, "log", lambda *a, **k: st["audit"].append(a))

    def go(**kw):
        base = dict(mode="generate", prompt="orders of today", sql="",
                    connection="srv-alpha", database="shop")
        base.update(kw)
        return ai.complete(ME, "Ex", ai.Ask(**base))
    go.st = st
    return go


def test_one_call_returns_the_answer_and_writes_one_audit_row(call):
    out = call(prompt="SECRET-PROMPT", sql="SELECT 'SECRET-SQL'", mode="fix")
    assert out["text"] == "SELECT 1;" and out["model"] == "1:claude-opus-5-5"
    assert out["sent"]["tableNames"] == 1
    assert "sk-SECRET-KEY" not in json.dumps(out)
    [(rid, uid, name, action, details)] = call.st["audit"]
    assert (rid, uid, action) == (None, ME, "ai_call")
    assert details["status"] == "ok" and details["prompt_chars"] == len("SECRET-PROMPT")
    blob = json.dumps(details)
    for secret in ("SECRET-PROMPT", "SECRET-SQL", "SELECT 1;", "sk-SECRET-KEY"):
        assert secret not in blob
    assert "public.orders" in call.st["calls"][0]["system"]


def test_off_means_no_provider_call(call):
    call.st["settings"] = _settings(enabled=False)
    with pytest.raises(ai.AIError) as got:
        call()
    assert got.value.status == 404 and not call.st["calls"] and not call.st["audit"]


@pytest.mark.parametrize("code,status", [("no_access", 403), ("unknown_connection", 404)])
def test_the_grant_check_runs_before_the_provider(call, code, status):
    call.st["grant"] = code
    with pytest.raises(ai.AIError) as got:
        call()
    assert got.value.status == status and not call.st["calls"]


def test_a_disabled_connection_is_unknown_to_a_non_admin(call, monkeypatch):
    off = SimpleNamespace(**{**PG.__dict__, "enabled": False})
    monkeypatch.setattr(schema_tools, "granted_database", lambda *a, **k: (off, {}))
    with pytest.raises(ai.AIError) as got:
        call()
    assert got.value.status == 404 and not call.st["calls"]


def test_the_limit_stops_the_call_before_the_provider(call):
    call.st["settings"] = _settings(max_calls_per_hour=1)
    call()
    with pytest.raises(ai.AIError) as got:
        call()
    assert got.value.status == 429 and len(call.st["calls"]) == 1


@pytest.mark.parametrize("kw,msg", [
    ({"mode": "drop"}, "Unknown mode"),
    ({"mode": "generate", "prompt": " "}, "Write what you need"),
    ({"mode": "explain", "sql": ""}, "There is no SQL"),
    ({"database": ""}, "Choose a connection"),
    ({"prompt": "x" * (ai.MAX_PROMPT_CHARS + 1)}, "prompt is too long"),
])
def test_bad_asks_are_refused_before_the_provider(call, kw, msg):
    with pytest.raises(ai.AIError) as got:
        call(**kw)
    assert got.value.status == 400 and msg in got.value.message and not call.st["calls"]


def test_a_chosen_model_needs_the_admin_toggle(call):
    two = _prov(models=("claude-opus-5-5", "claude-haiku-5-5"))
    call.st["providers"] = [two]
    call(model="1:claude-haiku-5-5")
    assert call.st["calls"][-1]["choice"].model == "claude-opus-5-5"     # toggle off
    call.st["settings"] = _settings(user_model_choice=True)
    call(model="1:claude-haiku-5-5")
    assert call.st["calls"][-1]["choice"].model == "claude-haiku-5-5"
    with pytest.raises(ai.AIError) as got:
        call(model="1:gpt-unknown")
    assert got.value.status == 400


def test_a_provider_with_no_tools_gets_no_toolbox(call):
    call.st["providers"] = [_prov(tools=False)]
    call()
    assert call.st["calls"][0]["toolbox"] is None


def test_a_refusal_is_said_and_audited(call):
    call.st["reply"] = ai.Reply(text="", stop="refusal", model="claude-opus-5-5")
    out = call()
    assert out["refused"] is True and out["note"]
    assert call.st["audit"][0][4]["status"] == "refused"


def test_a_failed_call_is_audited_with_its_code(call):
    call.st["reply"] = ai.AIError(502, "provider_auth", "The AI provider refused the API key.")
    with pytest.raises(ai.AIError):
        call()
    assert call.st["audit"][0][4]["status"] == "provider_auth"


def test_an_unexpected_crash_is_a_fixed_sentence(call):
    call.st["reply"] = RuntimeError("Authorization: Bearer sk-SECRET-KEY")
    with pytest.raises(ai.AIError) as got:
        call()
    assert "sk-SECRET" not in got.value.message
    assert call.st["audit"][0][4]["status"] == "error"


# --- admin input ---------------------------------------------------------------------


@pytest.mark.parametrize("kind,raw,want", [
    ("anthropic", "", None),
    ("anthropic", "https://proxy.example.test/", "https://proxy.example.test"),
    ("openai", "https://api.openai.com/v1/", "https://api.openai.com/v1"),
    ("openai", "http://localhost:11434/v1", "http://localhost:11434/v1"),
])
def test_good_base_urls(kind, raw, want):
    assert ai.clean_base_url(kind, raw) == want


@pytest.mark.parametrize("kind,raw", [
    ("openai", ""), ("openai", "ftp://x.test"), ("openai", "http://db.example.test/v1"),
    ("openai", "https://u:p@x.test/v1"), ("openai", "https://x.test/v1?k=1"),
    ("openai", "not a url"),
])
def test_bad_base_urls(kind, raw):
    with pytest.raises(ValueError):
        ai.clean_base_url(kind, raw)


def test_model_lists_are_checked_and_deduplicated():
    assert ai.clean_models("a, b ,a,,meta-llama/Llama-3.3-70B") == [
        "a", "b", "meta-llama/Llama-3.3-70B"]
    for bad in (["has space"], ["x" * 300], {"a": 1}):
        with pytest.raises(ValueError):
            ai.clean_models(bad)
    with pytest.raises(ValueError):
        ai.clean_models([f"m{i}" for i in range(ai.MAX_MODELS + 1)])


def test_a_provider_write_keeps_the_fields_it_does_not_name():
    cur = _prov(kind="openai", base_url="https://x.test/v1", models=("m",))
    out = ai.clean_provider({"enabled": False}, cur)
    assert out == {"name": "Anthropic", "kind": "openai", "base_url": "https://x.test/v1",
                   "models": ["m"], "supports_tools": True, "enabled": False}
    with pytest.raises(ValueError):
        ai.clean_provider({"name": "x", "kind": "gemini"})
    with pytest.raises(ValueError):
        ai.clean_provider({"name": "", "kind": "anthropic"})


@pytest.mark.parametrize("raw", ["", "a b", "x" * (ai.MAX_KEY_CHARS + 1), None])
def test_bad_keys(raw):
    with pytest.raises(ValueError):
        ai.clean_key(raw)


def test_settings_writes_are_checked():
    provs = [_prov(models=("claude-opus-5-5", "claude-haiku-5-5")),
             _prov(pid=2, kind="openai", base_url="https://x.test", models=("m",))]
    s = _settings(default_model="claude-haiku-5-5")
    got = ai.clean_settings({"enabled": False, "effort": "MAX", "maxCallsPerHour": 5,
                             "instructions": "x"}, provs, s)
    assert got == {"ai_enabled": "off", "ai_effort": "max",
                   "ai_max_calls_per_hour": "5", "ai_instructions": "x"}
    # A new provider without a model named drops a model it does not have.
    assert ai.clean_settings({"defaultProvider": 2}, provs, s) == {
        "ai_default_provider": "2", "ai_default_model": ""}
    for bad in ({"enabled": "yes"}, {"effort": "turbo"}, {"maxCallsPerHour": 0},
                {"timeoutSec": True}, {"defaultProvider": 9}, {"defaultModel": "m"},
                {"nope": 1}, {"instructions": "x" * (ai.MAX_INSTRUCTIONS_CHARS + 1)}):
        with pytest.raises(ValueError):
            ai.clean_settings(bad, provs, s)


class _Cur:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return {"id": 5}


def test_the_settings_audit_row_hides_the_instructions(monkeypatch):
    rows = []
    monkeypatch.setattr(audit, "log_in", lambda cur, *a: rows.append(a))
    cur = _Cur([{"key": "ai_enabled", "value": "off"}])
    changed = ai.save_settings(cur, {"ai_enabled": "off", "ai_instructions": "SECRET STYLE"},
                               ME, "Ex")
    assert changed == ["ai_instructions"]
    [(rid, uid, name, action, details)] = rows
    assert action == "ai_settings_set" and "SECRET" not in json.dumps(details)
    assert details["changes"] == [{"key": "ai_instructions", "to": "<12 characters>"}]


def test_a_new_key_is_stored_encrypted_and_never_audited(monkeypatch):
    rows = []
    monkeypatch.setattr(audit, "log_in", lambda cur, *a: rows.append(a))
    monkeypatch.setattr(ai.crypto, "encrypt", lambda plain: "CIPHER(" + plain[::-1] + ")")
    cur = _Cur()
    fields = ai.clean_provider({"name": "DeepSeek", "kind": "openai",
                                "baseUrl": "https://api.deepseek.com", "models": ["m"]})
    assert ai.create_provider(cur, fields, "sk-PLAIN", ME, "Ex") == 5
    params = cur.sql[0][1]
    assert "sk-PLAIN" not in json.dumps(params) and "CIPHER(" in json.dumps(params)
    assert rows[0][3] == "ai_provider_created" and "sk-PLAIN" not in json.dumps(rows[0])
    assert rows[0][4]["key"] == "set" and rows[0][4]["host"] == "api.deepseek.com"


@pytest.mark.parametrize("key,clear,want", [("sk-NEW", False, "set"),
                                            (None, True, "cleared"),
                                            (None, False, "unchanged")])
def test_a_provider_update_audits_the_key_change_only(monkeypatch, key, clear, want):
    rows = []
    monkeypatch.setattr(audit, "log_in", lambda cur, *a: rows.append(a))
    monkeypatch.setattr(ai.crypto, "encrypt", lambda plain: "CIPHER")
    cur = _Cur()
    current = _prov(kind="openai", base_url="https://x.test", models=("m",))
    ai.update_provider(cur, current, ai.clean_provider({"models": ["m", "n"]}, current),
                       key, clear, ME, "Ex")
    details = rows[0][4]
    assert details["key"] == want and details["changed"] == ["models"]
    assert "sk-NEW" not in json.dumps(rows) and "sk-NEW" not in json.dumps(cur.sql)


def test_the_provider_view_has_no_key():
    view = ai.provider_view(_prov())
    assert view["keySet"] is True
    assert not {k for k in view if "cipher" in k.lower() or k in ("apiKey", "key")}


def test_the_test_call_is_audited_and_reports_a_failure(monkeypatch):
    rows = []
    monkeypatch.setattr(audit, "log", lambda *a: rows.append(a))
    monkeypatch.setattr(ai, "providers", lambda: [_prov()])
    monkeypatch.setattr(ai, "settings", lambda: _settings())
    monkeypatch.setattr(ai, "_key_of", lambda pid: "k")

    def boom(*a, **k):
        raise ai.AIError(502, "provider_auth", "The AI provider refused the API key.")
    monkeypatch.setattr(ai, "_call", boom)
    out = ai.test_provider(ME, "Ex", 1)
    assert out["ok"] is False and out["code"] == "provider_auth"
    assert rows[0][3] == "ai_provider_tested" and rows[0][4]["status"] == "provider_auth"
    seen = {}

    def ok(choice, key, system, messages, toolbox, s, deadline):
        seen.update(toolbox=toolbox, messages=messages)
        return ai.Reply(text="OK", stop="end", model="claude-opus-5-5")
    monkeypatch.setattr(ai, "_call", ok)
    assert ai.test_provider(ME, "Ex", 1)["ok"] is True
    assert seen["toolbox"] is None and seen["messages"] == [{"role": "user", "content": "Say OK."}]


# --- who can use it -------------------------------------------------------------


def _rule(kind, value="", rid=1):
    return {"id": rid, "kind": kind, "value": value}


@pytest.fixture
def roles(monkeypatch):
    """Role and team answers for the audience tests."""
    st = {"super": set(), "dba": set(), "admin": set(), "approver": set(),
          "teams": {}, "team_reads": []}
    monkeypatch.setattr(ai.admins, "is_super_admin", lambda uid: uid in st["super"])
    monkeypatch.setattr(ai.admins, "is_dba", lambda uid: uid in st["dba"])
    monkeypatch.setattr(ai.admins, "is_admin", lambda uid: uid in st["admin"])
    monkeypatch.setattr(ai.admins, "holds_approval_role", lambda uid: uid in st["approver"])

    def team_ids(uid):
        st["team_reads"].append(uid)
        return st["teams"].get(uid, set())
    monkeypatch.setattr(ai, "_team_ids", team_ids)
    return st


OTHER = "U0EXAMPLE002"


def test_no_rule_means_nobody(roles):
    assert not ai.in_audience(ME, [])


def test_each_kind_of_rule(roles):
    roles["teams"] = {ME: {4}}
    roles["approver"] = {ME}
    assert ai.in_audience(ME, [_rule("everyone")])
    assert ai.in_audience(ME, [_rule("person", ME)])
    assert not ai.in_audience(OTHER, [_rule("person", ME)])
    assert ai.in_audience(ME, [_rule("team", "4")])
    assert not ai.in_audience(OTHER, [_rule("team", "4")])
    assert ai.in_audience(ME, [_rule("role", "approver")])
    assert not ai.in_audience(OTHER, [_rule("role", "approver")])


def test_mixed_rules_open_to_each_group(roles):
    roles["teams"] = {ME: {4}, "U0EXAMPLE003": {5}}
    roles["approver"] = {"U0EXAMPLE004"}
    rules = [_rule("team", "4", 1), _rule("team", "5", 2), _rule("person", OTHER, 3),
             _rule("role", "approver", 4)]
    for uid in (ME, OTHER, "U0EXAMPLE003", "U0EXAMPLE004"):
        assert ai.in_audience(uid, rules), uid
    assert not ai.in_audience("U0EXAMPLE005", rules)


@pytest.mark.parametrize("role,where", [("super_admin", "super"), ("dba", "dba"),
                                         ("admin", "admin"), ("approver", "approver")])
def test_each_role(roles, role, where):
    rules = [_rule("role", role)]
    assert not ai.in_audience(ME, rules)
    roles[where].add(ME)
    assert ai.in_audience(ME, rules)


def test_a_super_admin_counts_as_a_dba(roles):
    roles["super"].add(ME)
    assert ai.in_audience(ME, [_rule("role", "dba")])


def test_teams_are_read_only_when_they_can_decide(roles):
    ai.in_audience(ME, [_rule("team", "4", 1), _rule("person", ME, 2)])
    assert roles["team_reads"] == []
    ai.in_audience(OTHER, [_rule("team", "4", 1), _rule("person", ME, 2)])
    assert roles["team_reads"] == [OTHER]


def test_the_count_covers_requesters_and_admins(roles, monkeypatch):
    monkeypatch.setattr(ai.requesters, "list_enabled_ids", lambda: {ME, OTHER})
    monkeypatch.setattr(ai.admins, "list_active", lambda: [{"slack_user_id": "U0EXAMPLE003"}])
    roles["super"].add("U0EXAMPLE003")
    assert ai.audience_count([_rule("everyone")]) == 3
    assert ai.audience_count([_rule("role", "super_admin")]) == 1


def test_a_failed_count_says_nothing(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(ai.requesters, "list_enabled_ids", boom)
    assert ai.audience_count([_rule("everyone")]) is None


def test_audience_writes_are_checked(monkeypatch):
    monkeypatch.setattr(ai.db, "fetch_all",
                        lambda sql, params=None: [{"id": i} for i in params[0] if i in (4, 5)])
    assert ai.clean_audience("everyone", ["x"]) == ("everyone", [""])
    assert ai.clean_audience("person", [" U0EXAMPLE001 ", "U0EXAMPLE001", "local:sam.lee"]) == (
        "person", [ME, "local:sam.lee"])
    assert ai.clean_audience("team", ["4", "5"]) == ("team", ["4", "5"])
    assert ai.clean_audience("role", "dba") == ("role", ["dba"])
    for bad in (("group", ["x"]), ("person", []), ("person", ["bob"]),
                ("team", ["4", "9"]), ("team", ["x"]), ("role", ["owner"]),
                ("person", [f"U0EXAMPLE{i:03d}" for i in range(ai.MAX_AUDIENCE_VALUES + 1)])):
        with pytest.raises(ValueError):
            ai.clean_audience(*bad)


class _InsertCur:
    """A cursor whose INSERT ... RETURNING gives a row for new values only."""

    def __init__(self, existing=()):
        self.existing = set(existing)
        self.next_id = 10
        self.last = None

    def execute(self, sql, params=None):
        if "INSERT INTO ai_audience" in sql:
            kind, value = params[0], params[1]
            if (kind, value) in self.existing:
                self.last = None
            else:
                self.existing.add((kind, value))
                self.next_id += 1
                self.last = {"id": self.next_id}

    def fetchone(self):
        return self.last


def test_a_batch_adds_the_new_rules_and_audits_each(monkeypatch):
    rows = []
    monkeypatch.setattr(audit, "log_in", lambda cur, *a: rows.append(a))
    cur = _InsertCur(existing={("team", "4")})
    assert ai.add_audience(cur, "team", ["4", "5", "6"], ME, "Ex") == 2
    assert [r[3] for r in rows] == ["ai_audience_added", "ai_audience_added"]
    assert [r[4]["value"] for r in rows] == ["5", "6"]


def test_a_removal_is_audited(monkeypatch):
    rows = []
    monkeypatch.setattr(audit, "log_in", lambda cur, *a: rows.append(a))
    ai.remove_audience(_Cur(), {"id": 5, "kind": "team", "value": "4"}, ME, "Ex")
    assert rows[0][3] == "ai_audience_removed"
    assert rows[0][4] == {"rule_id": 5, "kind": "team", "value": "4"}


def test_a_call_outside_the_audience_is_refused_before_the_provider(call):
    call.st["audience"] = False
    with pytest.raises(ai.AIError) as got:
        call()
    assert got.value.status == 403 and "not open for you" in got.value.message
    assert not call.st["calls"]


def test_status_is_off_outside_the_audience(monkeypatch):
    monkeypatch.setattr(ai, "settings", lambda: _settings())
    monkeypatch.setattr(ai, "providers", lambda: [_prov()])
    monkeypatch.setattr(ai, "in_audience", lambda uid, rules=None: uid == ME)
    assert ai.status(ME)["enabled"] is True
    assert ai.status(OTHER) == {"enabled": False}
