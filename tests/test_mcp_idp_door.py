"""The MCP door of `/mcp`: the portal calls a tool for the person it asserts.

What these tests pin:

1. The catalog: the rules of QueryHub in the descriptions, the tools named
   as the portal lists them, a wait of at most 20 seconds, and a file that the
   portal embeds byte for byte.
2. `POST /mcp` with an assertion: the real `verify()` binds it to the method,
   the path and the exact body, and spends its `jti` once. The person comes
   from the same helper as the proxied `/api` routes, with the same codes.
   Every refusal is a JSON-RPC error with `data.code`, and no 401 starts a
   sign-in. Every answer names the catalog version.
3. The transport: 405 for a stream, the rate limit, the free slots, and a
   refusal or an error as a result that the model can read.
4. `/api/me` and `/sql claude` say how to connect while the door works.

Nothing here may reach a database.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI
from fastapi.testclient import TestClient

from queryhub import db, requesters
from queryhub.mcp_server import caller, catalog, policy, tools
from queryhub.slack_app import claude_cmd
from queryhub.web import admin as web_admin
from queryhub.web import app as web_app
from queryhub.web import deps, idp_assertion, routes_mcp

ROOT = Path(__file__).resolve().parents[1]
CATALOG_FILE = ROOT / "docs" / "mcp-tools.idp.json"

ME = "U0EXAMPLE001"
MY_ADDRESS = "dev@example.com"


# ---------------------------------------------------------------------------
# the catalog of the idp door
# ---------------------------------------------------------------------------

def test_the_catalog_file_is_the_generated_one():
    """The portal embeds a byte copy of this file. A change to a tool that
    does not reach the file never reaches the model on the idp door."""
    want = catalog.render()
    got = CATALOG_FILE.read_text(encoding="utf-8") if CATALOG_FILE.is_file() else ""
    assert got == want, (
        "docs/mcp-tools.idp.json is not the current catalog. Make it again with\n"
        "  python -m queryhub.mcp_server.catalog --door idp > docs/mcp-tools.idp.json\n"
        "and give the new file to the portal, which embeds a copy of it.")


def test_the_file_has_the_shape_that_the_portal_reads():
    raw = CATALOG_FILE.read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    assert list(doc) == ["version", "tools"]
    assert raw.endswith(b"}\n") and raw.startswith(b'{\n  "version": ')
    # The portal trusts `version` and does not compute it. Computed here
    # without the helper of the catalog, as the contract says.
    canonical = json.dumps(doc["tools"], sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    assert doc["version"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert doc["version"] == catalog.idp_version()


def test_the_listing_names_the_tools_bare():
    listed = catalog.listing()
    assert [t["name"] for t in listed] == [t.name for t in catalog.TOOLS]
    assert not any(t["name"].startswith("queryhub_") for t in listed), "the portal adds the prefix"


def test_the_wait_is_twenty_seconds():
    submit = next(t for t in catalog.listing() if t["name"] == "submit_query")
    wait = submit["inputSchema"]["properties"]["wait_seconds"]
    assert wait == {"type": "integer", "maximum": 20,
                    "description": "How long to wait for the result. Default 20. Maximum 20."}
    assert catalog.WAIT_SECONDS == tools.DEFAULT_WAIT_SECONDS == tools.MAX_WAIT_SECONDS == 20


@pytest.mark.parametrize("tool,rule", [
    ("submit_query", "Show the person `requestUrl`."),
    ("query_status", "Show the person `requestUrl`."),
    ("submit_query", "Do not submit the same statement again."),
    ("submit_query", "poll `queryhub_query_status`"),
    ("query_status", "Do not submit the statement again."),
    ("fetch_result", "While `resultMode` is `metadata_only`, you read no rows."),
    ("fetch_result", "Tell the person to open `requestUrl`"),
    ("submit_query", "A person always approves a write (RW) and a schema change (DDL)."),
    ("submit_query", "read the reason and change the statement"),
    ("list_connections", "Call this tool first."),
])
def test_the_descriptions_carry_the_rules(tool, rule):
    """The portal sends its own instructions, so the rules of QueryHub live in
    the descriptions."""
    described = {t["name"]: t["description"] for t in catalog.listing()}
    assert rule in described[tool]


BARE = re.compile("`(" + "|".join(t.name for t in catalog.TOOLS) + ")`")


def test_the_listing_names_each_tool_in_a_text_as_the_portal_lists_it():
    """The model sees only `queryhub_<name>`. A description that names a bare
    tool names one that it cannot call."""
    text = json.dumps(catalog.listing())
    assert BARE.findall(text) == []
    assert "`queryhub_list_connections`" in text and "`queryhub_query_status`" in text


def test_a_text_names_a_tool_with_the_prefix_of_the_portal():
    assert catalog.named("query_status") == "`queryhub_query_status`"


def test_no_answer_of_a_tool_names_a_tool_bare():
    """Every text of `tools` that names a tool asks `catalog.named`, so the
    door answers with the names that the portal lists."""
    tree = ast.parse(Path(tools.__file__).read_text(encoding="utf-8"))
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))
                  and n.body and isinstance(n.body[0], ast.Expr)}
    texts = [n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and id(n) not in docstrings]
    assert [t for t in texts if BARE.search(t)] == []


def test_a_wait_of_the_wrong_type_is_refused(monkeypatch):
    monkeypatch.setattr(tools, "submit_query", lambda **kw: pytest.fail("the tool ran"))
    with pytest.raises(tools.ToolError) as e:
        catalog.call("submit_query", {"connection": "c", "sql": "s", "database": "d",
                                      "wait_seconds": True})
    assert e.value.code == "bad_arguments"


def test_the_command_prints_the_file(capsys):
    assert catalog.main(["--door", "idp"]) == 0
    assert capsys.readouterr().out == catalog.render()
    assert catalog.main([]) == 0
    assert capsys.readouterr().out == catalog.render()


def test_the_command_needs_no_database_and_no_environment():
    """`python -m ... --door idp` runs with no env file."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run([sys.executable, "-m", "queryhub.mcp_server.catalog",
                           "--door", "idp"], env=env, capture_output=True, text=True,
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout == CATALOG_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the idp door
# ---------------------------------------------------------------------------

def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    return (
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()).decode(),
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    )


_JTI = iter(range(10**6))


def _assert(priv, body: bytes, *, sub=MY_ADDRESS, path="/mcp", method="POST"):
    """An assertion as the portal signs it."""
    now = int(time.time())
    claims = {"iss": "idp", "aud": "queryhub", "sub": sub, "jti": f"mcp-{next(_JTI)}",
              "iat": now, "exp": now + 60,
              "bh": idp_assertion.body_hash(method, path, body)}
    return pyjwt.encode(claims, priv, algorithm="EdDSA", headers={"kid": "k1"})


def _message(method="tools/call", params=None, mid=1) -> bytes:
    body = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        body["id"] = mid
    if params is not None:
        body["params"] = params
    return json.dumps(body, separators=(",", ":")).encode()


@pytest.fixture
def portal(monkeypatch):
    """The real `verify()` and the real policy behind `/mcp`. The settings,
    the two people tables and the jti ledger live in memory. `catalog.call` is
    a fake that records the call and the person that it runs as."""
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)
    routes_mcp._RATE._hits.clear()

    priv, pub = _keypair()
    settings = {"mcp_door_enabled": "on",
                "idp_assertion_enabled": "on", "idp_issuer": "idp",
                "idp_audience": "queryhub", "web_allowed_email_domain": "example.com",
                "idp_public_keys": json.dumps({"k1": pub})}
    monkeypatch.setattr(policy.cfg, "get_setting", lambda k, d=None: settings.get(k, d))

    used = set()

    def claim(jti, until):
        if jti in used:
            return False
        used.add(jti)
        return True
    monkeypatch.setattr(idp_assertion, "_claim_jti", claim)

    people = {"requesters": [{"slack_user_id": ME, "email": MY_ADDRESS,
                              "name": "Ex Ample", "enabled": True}], "admins": []}

    def fetch_all(sql, params=None):
        rows = people["requesters"] if "FROM requesters" in sql else people["admins"]
        return [r for r in rows if r["email"] == params[0]]
    monkeypatch.setattr(requesters.db, "fetch_all", fetch_all)
    allowed = {ME}
    monkeypatch.setattr(routes_mcp.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(routes_mcp.requesters, "is_allowed", lambda uid: uid in allowed)

    calls = []

    def fake_call(name, arguments):
        calls.append((name, arguments, caller._BOUND.get()))
        return {"fine": True}
    monkeypatch.setattr(catalog, "call", fake_call)

    app = FastAPI()
    app.include_router(routes_mcp.router)
    st = SimpleNamespace(client=TestClient(app), priv=priv, settings=settings, used=used,
                         people=people, allowed=allowed, calls=calls)

    def post(body: bytes, token=None, headers=None, client=None):
        sent = {"content-type": "application/json",
                deps.ASSERTION_HEADER: token or _assert(priv, body)}
        sent.update(headers or {})
        return (client or st.client).post("/mcp", content=body, headers=sent)
    st.post = post
    return st


def _refusal(r):
    err = r.json()["error"]
    assert r.json()["id"] is None and err["code"] == -32000
    return err["data"]["code"], err["message"]


def _result(r):
    res = r.json()["result"]
    return json.loads(res["content"][0]["text"]), res["isError"]


def test_tools_list_answers_the_idp_listing_and_its_version(portal):
    r = portal.post(_message("tools/list"))
    assert r.status_code == 200, r.text
    assert r.json() == {"jsonrpc": "2.0", "id": 1,
                        "result": {"tools": catalog.listing()}}
    assert r.headers[routes_mcp.CATALOG_HEADER] == catalog.idp_version()
    assert r.headers[routes_mcp.CATALOG_HEADER] == json.loads(
        CATALOG_FILE.read_text(encoding="utf-8"))["version"]


def test_a_tool_runs_as_the_person_of_the_assertion(portal, monkeypatch):
    """The door binds the person in a worker thread. The check after the call
    runs in that thread too: the test thread never sees the binding."""
    after = []
    real = routes_mcp._call_tool

    def spy(uid, name, arguments):
        out = real(uid, name, arguments)
        after.append(caller._BOUND.get())
        return out
    monkeypatch.setattr(routes_mcp, "_call_tool", spy)
    r = portal.post(_message(params={"name": "list_connections", "arguments": {}}))
    assert r.status_code == 200, r.text
    assert _result(r) == ({"fine": True}, False)
    assert portal.calls == [("list_connections", {}, ME)]
    assert r.headers[routes_mcp.CATALOG_HEADER] == catalog.idp_version()
    assert after == [None], "the person is unbound after the call"


def test_ping_works(portal):
    r = portal.post(_message("ping"))
    assert r.status_code == 200 and r.json()["result"] == {}


def test_a_body_changed_after_signing_is_refused(portal):
    signed = _message(params={"name": "list_connections", "arguments": {}})
    changed = _message(params={"name": "submit_query",
                               "arguments": {"connection": "c", "sql": "drop table t"}})
    r = portal.post(changed, token=_assert(portal.priv, signed))
    assert r.status_code == 401
    assert _refusal(r) == ("unauthenticated", "Refused: the identity assertion is not valid.")
    assert portal.calls == []


def test_an_assertion_for_another_path_is_refused(portal):
    body = _message("tools/list")
    for path in ("/api/queries", "/mcp?x=1", "/api/mcp"):
        r = portal.post(body, token=_assert(portal.priv, body, path=path))
        assert r.status_code == 401, path
        assert _refusal(r)[0] == "unauthenticated"


def test_an_assertion_for_another_method_is_refused(portal):
    body = _message("tools/list")
    r = portal.post(body, token=_assert(portal.priv, body, method="PUT"))
    assert r.status_code == 401


def test_an_assertion_works_once(portal):
    body = _message(params={"name": "list_connections", "arguments": {}})
    token = _assert(portal.priv, body)
    assert portal.post(body, token=token).status_code == 200
    again = portal.post(body, token=token)
    assert again.status_code == 401 and _refusal(again)[0] == "unauthenticated"
    assert len(portal.calls) == 1


def test_a_forged_assertion_learns_nothing(portal):
    other, _pub = _keypair()
    for sub in (MY_ADDRESS, "stranger@example.com"):
        body = _message("tools/list")
        r = portal.post(body, token=_assert(other, body, sub=sub))
        assert r.status_code == 401 and _refusal(r)[0] == "unauthenticated", sub


@pytest.mark.parametrize("sub,code", [
    ("stranger@example.com", "not_onboarded"),
    ("dev@elsewhere.test", "bad_domain"),
])
def test_a_refusal_about_the_person_is_403_with_the_seam_code(portal, sub, code):
    body = _message("tools/list")
    r = portal.post(body, token=_assert(portal.priv, body, sub=sub))
    assert r.status_code == 403
    assert _refusal(r) == (code, idp_assertion.PERSON_MESSAGES[code])
    assert r.headers[routes_mcp.CATALOG_HEADER] == catalog.idp_version()


def test_a_shared_address_is_403_ambiguous_address(portal):
    portal.people["admins"].append({"slack_user_id": "U0EXAMPLE002", "email": MY_ADDRESS,
                                    "name": "Other", "enabled": True})
    r = portal.post(_message("tools/list"))
    assert r.status_code == 403 and _refusal(r)[0] == "ambiguous_address"


def test_a_person_who_lost_access_is_refused_before_any_message(portal):
    """The resolver also finds a disabled row."""
    portal.allowed.clear()
    r = portal.post(_message(params={"name": "list_connections", "arguments": {}}))
    assert r.status_code == 403 and _refusal(r)[0] == "not_whitelisted"
    assert portal.calls == []


def test_no_401_of_this_door_starts_a_sign_in(portal):
    body = _message("tools/list")
    for token in ("not-a-jwt", _assert(_keypair()[0], body)):
        r = portal.post(body, token=token)
        assert r.status_code == 401
        assert "www-authenticate" not in r.headers


def test_both_credentials_are_refused_before_the_assertion_is_spent(portal, monkeypatch):
    monkeypatch.setattr(idp_assertion, "verify",
                        lambda *a, **k: pytest.fail("verify() ran with a second credential"))
    r = portal.post(_message("tools/list"), headers={"authorization": "Bearer a-token"})
    assert r.status_code == 400 and _refusal(r)[0] == "ambiguous_credentials"
    assert portal.used == set()


def test_a_list_of_messages_is_refused(portal):
    body = json.dumps([json.loads(_message("ping")), json.loads(_message("tools/list", mid=2))]).encode()
    r = portal.post(body)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600
    assert portal.calls == []


def test_bad_json_is_a_parse_error(portal):
    r = portal.post(b"{nope")
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700


@pytest.mark.parametrize("method", ["initialize", "resources/list", "prompts/get"])
def test_other_methods_are_not_found(portal, method):
    r = portal.post(_message(method, {"protocolVersion": "2025-06-18"}))
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32601
    assert r.headers[routes_mcp.CATALOG_HEADER] == catalog.idp_version()


def test_a_notification_gets_202_with_the_version(portal):
    r = portal.post(_message("notifications/initialized", mid=None))
    assert r.status_code == 202 and r.content == b""
    assert r.headers[routes_mcp.CATALOG_HEADER] == catalog.idp_version()


def test_an_unknown_protocol_version_is_refused(portal):
    r = portal.post(_message("tools/list"), headers={"mcp-protocol-version": "2099-01-01"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022


def test_the_door_passes_the_arguments_as_they_came(portal):
    """The tool clamps the wait itself (see the next test), so the door
    changes no argument."""
    arguments = {"connection": "c", "sql": "select 1", "database": "d", "wait_seconds": 60}
    r = portal.post(_message(params={"name": "submit_query", "arguments": arguments}))
    assert r.status_code == 200
    assert portal.calls == [("submit_query", arguments, ME)]


@pytest.mark.parametrize("asked", [None, 20, 60])
@pytest.mark.parametrize("before", [0, 6, 25])
def test_the_wait_counts_from_the_start_of_the_call(monkeypatch, before, asked):
    """The portal cancels its call 25 seconds after it sends it. The checks,
    the pre-flight plan and the notices use seconds before the wait starts,
    so the 20 seconds count from the start of the call. A budget that they
    used up still reads the status once. A call that asks for longer waits
    20 seconds too."""
    clock = SimpleNamespace(now=1000.0, polls=0)

    def sleep(seconds):
        clock.now += seconds

    def fetch_one(sql, params=None):
        clock.polls += 1
        return {"status": "executing"}

    def notify(*a, **k):
        clock.now += before
    monkeypatch.setattr(tools, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(tools.db, "fetch_one", fetch_one)
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools, "_target_or_refuse", lambda uid, c: SimpleNamespace(
        id=7, alias="example-pg", engine="postgres"))
    monkeypatch.setattr(tools.caller, "display_name", lambda uid: "Ex")
    monkeypatch.setattr(tools.core_submit, "validate_submission", lambda *a, **k: object())
    monkeypatch.setattr(tools.core_submit, "create_request", lambda prep: SimpleNamespace(
        auto_approved=True, row={"id": 41, "status": "approved"}))
    monkeypatch.setattr(tools.core_submit, "dispatch_and_notify", notify)
    monkeypatch.setattr(policy, "max_tier", lambda: "ro")
    monkeypatch.setattr(policy.cfg, "get_setting", lambda k, d=None: d)

    kwargs = {} if asked is None else {"wait_seconds": asked}
    out = tools.submit_query("example-pg", database="app", sql="select 1", **kwargs)
    assert out["status"] == "executing" and "The wait ended" in out["note"]
    spent = clock.now - 1000.0
    assert max(before, catalog.WAIT_SECONDS) <= spent < max(before, catalog.WAIT_SECONDS) + 1
    assert clock.polls >= 1


def test_a_refused_tool_call_is_a_result_the_model_can_read(portal, monkeypatch):
    def refuse(name, arguments):
        raise tools.ToolError("blocked", "Read the reason.")
    monkeypatch.setattr(catalog, "call", refuse)
    r = portal.post(_message(params={"name": "submit_query",
                                     "arguments": {"connection": "c", "sql": "x"}}))
    assert r.status_code == 200
    assert _result(r) == ({"error": "blocked", "message": "Read the reason."}, True)


def test_an_answer_names_the_tools_as_the_portal_lists_them(monkeypatch):
    """A refusal tells the model which tool to call next, with the name that
    the model can call through the portal."""
    monkeypatch.setattr(policy.cfg, "get_setting",
                        lambda k, d=None: "on" if k == "mcp_door_enabled" else d)
    monkeypatch.setattr(caller.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(caller.requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(tools.targets, "by_alias", lambda alias: None)
    routes_mcp._RATE._hits.clear()
    out = routes_mcp._call_tool(ME, "describe_database",
                                {"connection": "nope", "database": "app"})
    body = json.loads(out["content"][0]["text"])
    assert out["isError"] is True and body["error"] == "unknown_connection"
    assert "Call `queryhub_list_connections` to see your connections." in body["message"]


def test_the_log_names_the_door_and_no_argument(portal, caplog):
    caplog.set_level("INFO")
    portal.post(_message(params={"name": "submit_query",
                                 "arguments": {"connection": "c", "sql": "select 'secret-value'"}}))
    assert "door=idp" in caplog.text and "tool=submit_query" in caplog.text
    assert "secret-value" not in caplog.text


def test_the_master_switch_closes_this_door(portal):
    portal.settings["mcp_door_enabled"] = "off"
    r = portal.post(_message("tools/list"))
    assert r.status_code == 503 and _refusal(r)[0] == "disabled"
    assert portal.used == set(), "no assertion is spent"
    assert r.headers[routes_mcp.CATALOG_HEADER] == catalog.idp_version()


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer a-token"}])
def test_a_call_without_the_portal_is_refused(portal, headers):
    """Only the portal calls this door. A call with no assertion gets 401 and
    learns where to connect Claude Code."""
    r = portal.client.post("/mcp", content=_message("tools/list"),
                           headers={"content-type": "application/json", **headers})
    assert r.status_code == 401
    code, message = _refusal(r)
    assert code == "unauthenticated" and "Connect Claude Code" in message
    assert "www-authenticate" not in r.headers
    assert portal.calls == []


def test_there_is_no_stream_and_no_session_to_end(portal):
    for method in ("GET", "DELETE"):
        r = portal.client.request(method, "/mcp")
        assert r.status_code == 405 and r.headers["allow"] == "POST", method
    assert "mcp-session-id" not in portal.post(_message("ping")).headers


def _tool_call(portal, tool="list_connections", arguments=None):
    return portal.post(_message(params={"name": tool, "arguments": arguments or {}}))


def test_the_own_ui_switch_does_not_judge_an_mcp_call(portal, monkeypatch):
    """MCP follows Slack, not the web UI."""
    def boom(*a, **k):
        raise AssertionError("own_ui_allowed ran for an MCP call")
    monkeypatch.setattr(deps, "own_ui_allowed", boom)
    assert _result(_tool_call(portal)) == ({"fine": True}, False)


def test_a_caller_error_is_a_result_too(portal, monkeypatch):
    def refuse(name, arguments):
        raise caller.CallerError("not_whitelisted", "Ask the DBA team.")
    monkeypatch.setattr(catalog, "call", refuse)
    assert _result(_tool_call(portal))[0]["error"] == "not_whitelisted"


def test_an_unexpected_error_leaks_nothing(portal, monkeypatch):
    def boom(name, arguments):
        raise RuntimeError("password=hunter2 in the traceback")
    monkeypatch.setattr(catalog, "call", boom)
    r = _tool_call(portal)
    assert r.status_code == 200 and "hunter2" not in r.text
    assert _result(r)[0]["error"] == "server_error"


def test_the_rate_limit_stops_the_third_call(portal, monkeypatch):
    monkeypatch.setattr(routes_mcp, "_limit_per_minute", lambda: 2)
    errors = [_result(_tool_call(portal))[0].get("error") for _ in range(3)]
    assert errors == [None, None, "rate_limited"]


def test_the_rate_limit_is_per_person(monkeypatch):
    monkeypatch.setattr(routes_mcp, "_limit_per_minute", lambda: 1)
    monkeypatch.setattr(catalog, "call", lambda name, arguments: {"fine": True})
    routes_mcp._RATE._hits.clear()
    assert routes_mcp._call_tool(ME, "list_connections", {})["isError"] is False
    assert routes_mcp._call_tool(ME, "list_connections", {})["isError"] is True
    assert routes_mcp._call_tool("U0EXAMPLE002", "list_connections", {})["isError"] is False


def test_too_many_calls_at_once_get_busy(portal, monkeypatch):
    class Full:
        def acquire(self, blocking=True):
            return False
    monkeypatch.setattr(routes_mcp, "_SLOTS", Full())
    assert _result(_tool_call(portal)) == (
        {"error": "busy", "message": "QueryHub is busy. Try again in a moment."}, True)


def test_a_tool_call_holds_a_thread_of_the_door(portal, monkeypatch):
    """A call can wait 20 seconds. It holds a thread of the door, not one of
    the pool that the web routes share."""
    held = []

    def answer(message, uid):
        held.append(routes_mcp._door_threads().borrowed_tokens)
        return {"jsonrpc": "2.0", "id": message["id"], "result": {}}
    monkeypatch.setattr(routes_mcp, "_answer", answer)
    assert portal.post(_message(method="ping")).status_code == 200
    assert held == [1]
    assert routes_mcp._door_threads().total_tokens == routes_mcp.DOOR_THREADS
    assert routes_mcp.DOOR_THREADS > routes_mcp.MAX_CONCURRENT_CALLS


def test_the_door_finds_the_person_with_the_helper_of_the_portal_seam():
    """The same rules and codes as the proxied /api routes, not a copy."""
    import inspect
    src = inspect.getsource(routes_mcp._idp_door)
    assert "deps.assertion_claims(" in src or "deps.assertion_claims," in src
    assert "idp_assertion.verify" not in src
    assert '"POST"' in src and "deps._request_target(request)" in src


def test_the_door_works_behind_the_real_app(portal):
    """The app caches the body of an assertion request in a middleware. The
    route must still read the same bytes."""
    client = TestClient(web_app.create_app())
    body = _message(params={"name": "list_connections", "arguments": {}})
    r = portal.post(body, client=client)
    assert r.status_code == 200, r.text
    assert _result(r) == ({"fine": True}, False)
    assert r.headers[routes_mcp.CATALOG_HEADER] == catalog.idp_version()


# ---------------------------------------------------------------------------
# how a person connects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("enabled,want", [("on", {"mode": "idp"}), ("off", None)])
def test_me_says_how_to_connect_while_the_door_works(portal, monkeypatch, enabled, want):
    """GET /api/me from the portal, through the real app."""
    portal.settings["mcp_door_enabled"] = enabled
    monkeypatch.setattr(web_admin, "admin_block", lambda uid: None)
    client = TestClient(web_app.create_app())
    token = _assert(portal.priv, b"", path="/api/me", method="GET")
    r = client.get("/api/me", headers={deps.ASSERTION_HEADER: token})
    assert r.status_code == 200, r.text
    assert r.json().get("claudeMcp") == want


def test_without_the_assertions_the_portal_cannot_connect(portal):
    """The door needs the portal to prove the person. The own web UI can
    still ask /api/me then."""
    portal.settings["idp_assertion_enabled"] = "off"
    assert routes_mcp.connect_info() is None


def test_connect_info_answers_when_the_settings_cannot_be_read(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("no database")
    monkeypatch.setattr(routes_mcp.policy, "enabled", broken)
    assert routes_mcp.connect_info() is None, "/api/me must still answer"


@pytest.fixture
def slack(portal, monkeypatch):
    said = []
    monkeypatch.setattr(claude_cmd.admins, "is_admin", lambda u: False)
    monkeypatch.setattr(claude_cmd.requesters, "is_allowed", lambda u: True)

    def run(rest=""):
        claude_cmd.handle(ME, rest, None, lambda m: said.append(m["text"]), {})
        return said[-1]
    return run


def test_slack_sends_the_person_to_the_portal(slack):
    text = slack()
    assert "Open QueryHub in the portal." in text and "Select Connect Claude Code." in text
    assert "claude mcp add" not in text and "Approve" not in text


def test_slack_names_the_missing_switch(portal, slack):
    portal.settings["idp_assertion_enabled"] = "off"
    assert "`idp_assertion_enabled`" in slack()


def test_slack_is_off_while_the_door_is_off(portal, slack):
    portal.settings["mcp_door_enabled"] = "off"
    assert "off" in slack()


def test_slack_refuses_a_person_who_is_not_whitelisted(slack, monkeypatch):
    monkeypatch.setattr(claude_cmd.requesters, "is_allowed", lambda u: False)
    assert "not whitelisted" in slack()


@pytest.mark.parametrize("rest", ["devices", "revoke 12", "abcd-efgh"])
def test_slack_has_no_subcommand(slack, rest):
    """QueryHub keeps no row for a token of the portal, so there is nothing
    to list or to end here."""
    assert slack(rest).startswith("Usage: `/sql claude`.")


def test_the_help_line_says_how():
    assert claude_cmd.DESCRIPTION == "Connect Claude Code to QueryHub. `/sql claude` says how."
