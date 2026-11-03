"""The idp door of `/mcp` and the answer to a second submit, on a real PostgreSQL.

The unit tests fake the SQL. These run it: migration 201 seeds the switches, the
`jti` ledger refuses a replay, the two people tables resolve the address, and
the lookup of a waiting request finds the row of the duplicate guard and
nothing else. The assertions are real Ed25519 tokens, signed with a test key.

Every row belongs to names that no real target or person has. The tests
remove their people and requests. The target row stays behind. Run this only
against a scratch database.
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI
from fastapi.testclient import TestClient

from queryhub import core_submit, db, query_safety, targets
from queryhub.mcp_server import policy, tools
from queryhub.web import deps, idp_assertion, routes_mcp

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

PERSON, OTHER = "U0ITIDP001", "U0ITIDP002"
ADDRESS = "it-idp@example.test"
ALIAS = "it-idp-main"
MARK = "it_idp_marker_5521"


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


def _wipe():
    db.execute("DELETE FROM requests WHERE requester_slack_id = ANY(%s) AND query LIKE %s",
               ([PERSON, OTHER], f"%{MARK}%"))
    db.execute("DELETE FROM requesters WHERE slack_user_id = ANY(%s)", ([PERSON, OTHER],))


@pytest.fixture
def world(monkeypatch):
    _wipe()
    db.execute("INSERT INTO requesters (slack_user_id, name, email, enabled) "
               "VALUES (%s, 'IT IdP Person', %s, TRUE), (%s, 'IT IdP Other', NULL, TRUE)",
               (PERSON, ADDRESS, OTHER))
    tid = (db.fetch_one("SELECT id FROM target_servers WHERE alias = %s", (ALIAS,)) or
           db.insert_returning(
               "INSERT INTO target_servers (alias, host, default_database, username, "
               "  password_encrypted, engine, enabled) "
               "VALUES (%s, 'idp.example.internal', 'app', 'unused', 'unused', 'postgres', TRUE) "
               "RETURNING id", (ALIAS,)))["id"]
    priv, pub = _keypair()
    settings = {"mcp_door_enabled": "on",
                "idp_assertion_enabled": "on", "idp_issuer": "idp",
                "idp_audience": "queryhub", "web_allowed_email_domain": "example.test",
                "idp_public_keys": json.dumps({"k1": pub})}
    real = policy.cfg.get_setting
    monkeypatch.setattr(policy.cfg, "get_setting",
                        lambda k, d=None: settings[k] if k in settings else real(k, d))
    routes_mcp._RATE._hits.clear()
    app = FastAPI()
    app.include_router(routes_mcp.router)
    yield SimpleNamespace(client=TestClient(app), priv=priv, tid=tid,
                          target=targets.get(tid))
    _wipe()


_JTI = iter(range(10**6))


def _assert(priv, body: bytes, sub=ADDRESS) -> str:
    now = int(time.time())
    claims = {"iss": "idp", "aud": "queryhub", "sub": sub,
              "jti": f"it-idp-{os.getpid()}-{now}-{next(_JTI)}",
              "iat": now, "exp": now + 60, "bh": idp_assertion.body_hash("POST", "/mcp", body)}
    return pyjwt.encode(claims, priv, algorithm="EdDSA", headers={"kid": "k1"})


def _call(world, tool="list_connections", arguments=None, token=None, sub=ADDRESS):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments or {}}}).encode()
    return world.client.post("/mcp", content=body, headers={
        "content-type": "application/json",
        deps.ASSERTION_HEADER: token or _assert(world.priv, body, sub)})


def _request(world, query, status="pending", who=PERSON, database="app"):
    return db.insert_returning(
        "INSERT INTO requests (requester_slack_id, requester_name, target_server_id, "
        "  database_name, query, status, origin, required_tier, engine) "
        "VALUES (%s, 'IT IdP Person', %s, %s, %s, %s, 'mcp', 'ro', 'postgres') RETURNING id",
        (who, world.tid, database, query, status))["id"]


def test_the_migration_seeds_the_switches_of_the_door():
    rows = db.fetch_all("SELECT key FROM bot_config WHERE key = ANY(%s)",
                        (["mcp_door_enabled", "mcp_max_tier", "mcp_rate_limit_per_minute",
                          "mcp_portal_url", "mcp_result_mode"],))
    assert len(rows) == 5


def test_a_tool_call_runs_for_the_person_of_the_assertion(world):
    r = _call(world)
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    body = json.loads(res["content"][0]["text"])
    assert res["isError"] is False and body["maxTier"] == "RO"
    assert r.headers[routes_mcp.CATALOG_HEADER] == routes_mcp.catalog.idp_version()


def test_the_ledger_refuses_a_replayed_assertion(world):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "list_connections", "arguments": {}}}).encode()
    token = _assert(world.priv, body)
    assert _call(world, token=token).status_code == 200
    again = _call(world, token=token)
    assert again.status_code == 401
    assert again.json()["error"]["data"]["code"] == "unauthenticated"


def test_an_unknown_address_is_not_onboarded(world):
    r = _call(world, sub="nobody-it-idp@example.test")
    assert r.status_code == 403 and r.json()["error"]["data"]["code"] == "not_onboarded"


def test_a_disabled_requester_is_refused_before_any_tool(world):
    db.execute("UPDATE requesters SET enabled = FALSE WHERE slack_user_id = %s", (PERSON,))
    r = _call(world)
    assert r.status_code == 403 and r.json()["error"]["data"]["code"] == "not_whitelisted"


def test_the_lookup_finds_the_request_of_the_duplicate_guard(world):
    sql = f"select '{MARK}_1'"
    rid = _request(world, sql)
    # The guard of core_submit, inside its own transaction, as a submit runs it.
    prep = SimpleNamespace(user_id=PERSON, target=world.target, database="app", query=sql)
    with db.transaction() as cur:
        refused = core_submit._recheck_open_limits(cur, prep, None)
    assert refused is not None and refused.reason == "duplicate" and f"#{rid}" in refused.message
    # The same row, found the way submit_query finds it: no database names the
    # default one, and the statement is stripped.
    assert tools._open_request(PERSON, world.target, None, f"  {sql}  ")["id"] == rid
    out = tools._duplicate_or_refuse(refused, PERSON, world.target, None, sql)
    assert (out["requestId"], out["duplicate"], out["decision"]) == (rid, True, "needs_approval")


def test_the_lookup_finds_nothing_else(world):
    sql = f"select '{MARK}_2'"
    _request(world, sql, status="completed")
    _request(world, sql, who=OTHER)
    _request(world, sql, database="other")
    _request(world, sql + " ")   # stored stripped by a submit, so never like this
    assert tools._open_request(PERSON, world.target, "app", sql) is None
    assert tools._open_request(PERSON, world.target, "app", f"select '{MARK}_%'") is None


def test_a_statement_with_a_password_keeps_the_refusal(world):
    """Two statements that differ only in a password have one stored form.
    The guard refuses the second, and the lookup gives no row for either."""
    first = f"select id from {MARK} where password = 'it-a'"
    second = f"select id from {MARK} where password = 'it-b'"
    stored = query_safety.mask_password_literals(first)
    assert stored == query_safety.mask_password_literals(second) and "it-a" not in stored
    _request(world, stored)
    prep = SimpleNamespace(user_id=PERSON, target=world.target, database="app", query=second)
    with db.transaction() as cur:
        refused = core_submit._recheck_open_limits(cur, prep, None)
    assert refused is not None and refused.reason == "duplicate"
    assert tools._open_request(PERSON, world.target, "app", first) is None
    assert tools._open_request(PERSON, world.target, "app", second) is None
    with pytest.raises(tools.ToolError) as e:
        tools._duplicate_or_refuse(refused, PERSON, world.target, "app", second)
    assert e.value.code == "duplicate"
