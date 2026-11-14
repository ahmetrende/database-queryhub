"""The HTTP side of the AI assistant (M13, PR B) and the `ai_assisted` mark.

- `/api/ai/*` is for a whitelisted person. `/api/admin/ai*` is for a
  super-admin only, because it holds the API keys.
- A key goes in and never comes out.
- `POST /queries` keeps `aiAssisted` only while the assistant is on. The mark
  reaches the stored row, the Slack card, the history and the queue.
"""
from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from queryhub import ai, targets
from queryhub import core_submit as cs
from queryhub.slack_app import notifications
from queryhub.web import admin as admin_mod
from queryhub.web import app as web_app
from queryhub.web import deps, mapping, routes_ai, routes_queries

ROOT = Path(__file__).resolve().parents[1]
SUPER = "U0EXAMPLE001"
PLAIN = "U0EXAMPLE002"


def _prov(**kw) -> ai.Provider:
    base = dict(id=1, name="Anthropic", kind="anthropic", base_url=None,
                models=("claude-opus-5-5",), supports_tools=True, enabled=True,
                key_set=True, key_set_by=SUPER)
    base.update(kw)
    return ai.Provider(**base)


@pytest.fixture
def web(monkeypatch):
    logging.disable(logging.CRITICAL)
    st = {"uid": SUPER, "super": True, "tx": [], "providers": [_prov()],
          "settings": ai.Settings(enabled=True, default_provider=1, default_model="",
                                  user_model_choice=False, effort="", instructions="",
                                  max_calls_per_hour=30, timeout_sec=60,
                                  schema_max_tables=200)}
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": st["uid"], "provider": "slack", "sid": "s1", "name": "Ex"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(admin_mod.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(admin_mod.admins, "is_super_admin", lambda uid: st["super"])
    monkeypatch.setattr(ai, "settings", lambda: st["settings"])
    monkeypatch.setattr(ai, "providers", lambda: st["providers"])
    st["rules"] = [{"id": 1, "kind": "role", "value": "super_admin",
                    "created_by": "migration 211"}]
    monkeypatch.setattr(ai, "audience_rules", lambda: st["rules"])
    monkeypatch.setattr(ai, "audience_count", lambda rules: 3)
    monkeypatch.setattr(ai, "in_audience", lambda uid, rules=None: uid == SUPER)
    monkeypatch.setattr(ai, "get_provider",
                        lambda pid: next((p for p in st["providers"] if p.id == pid), None))

    @contextlib.contextmanager
    def tx():
        cur = object()
        st["tx"].append(cur)
        yield cur
    monkeypatch.setattr(routes_ai.db, "transaction", tx)
    c = TestClient(app, raise_server_exceptions=False)
    c.st = st
    yield c
    logging.disable(logging.NOTSET)


def _err(r):
    body = r.json()
    return body.get("error") or body.get("detail") or body


def test_status_follows_the_audience(web):
    assert web.get("/api/ai/status").json()["enabled"] is True
    # A whitelisted person outside the audience sees it off.
    web.st["uid"] = PLAIN
    r = web.get("/api/ai/status")
    assert r.status_code == 200 and r.json() == {"enabled": False}


def test_complete_maps_a_refusal_to_its_status(web, monkeypatch):
    def refuse(uid, name, ask):
        assert (ask.connection, ask.database, ask.mode) == ("srv-alpha", "shop", "explain")
        raise ai.AIError(403, "forbidden", "You have no access to that database.")
    monkeypatch.setattr(ai, "complete", refuse)
    r = web.post("/api/ai/complete", json={"mode": "explain", "sql": "SELECT 1",
                                           "connectionId": "srv-alpha", "databaseId": "shop"})
    assert r.status_code == 403
    assert _err(r) == {"code": "forbidden", "message": "You have no access to that database."}


def test_complete_passes_the_turns(web, monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "complete",
                        lambda uid, name, ask: seen.update(ask=ask, uid=uid) or {"text": "ok"})
    r = web.post("/api/ai/complete", json={
        "mode": "chat", "prompt": "p", "connectionId": "c", "databaseId": "d",
        "messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]})
    assert r.status_code == 200 and seen["uid"] == SUPER
    assert seen["ask"].messages == [{"role": "user", "content": "a"},
                                    {"role": "assistant", "content": "b"}]


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/admin/ai", None),
    ("put", "/api/admin/ai", {"settings": {"enabled": True}}),
    ("post", "/api/admin/ai/providers", {"name": "x", "kind": "anthropic"}),
    ("put", "/api/admin/ai/providers/1", {"enabled": False}),
    ("delete", "/api/admin/ai/providers/1", None),
    ("post", "/api/admin/ai/test", {"providerId": 1}),
])
def test_the_admin_routes_are_for_a_super_admin(web, method, path, body):
    web.st["super"] = False
    r = getattr(web, method)(path, **({"json": body} if body is not None else {}))
    assert r.status_code == 403 and _err(r)["code"] == "forbidden"
    assert not web.st["tx"]


def test_the_admin_view_has_no_key(web):
    r = web.get("/api/admin/ai")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True and body["providers"][0]["keySet"] is True
    assert [p["name"] for p in body["presets"]][:2] == ["Anthropic", "OpenAI"]
    assert "api_key" not in json.dumps(body) and "apiKey" not in json.dumps(body)


def test_a_new_provider_sends_its_key_to_the_store_only(web, monkeypatch):
    seen = {}

    def create(cur, fields, api_key, uid, name):
        seen.update(fields=fields, key=api_key, uid=uid)
        return 9
    monkeypatch.setattr(ai, "create_provider", create)
    r = web.post("/api/admin/ai/providers", json={
        "name": "DeepSeek", "kind": "openai", "baseUrl": "https://api.deepseek.com",
        "models": "deepseek-chat", "apiKey": " sk-PLAIN "})
    assert r.status_code == 201, r.text
    assert seen["key"] == "sk-PLAIN" and seen["fields"]["models"] == ["deepseek-chat"]
    assert "sk-PLAIN" not in r.text and r.json()["id"] == 9


def test_a_bad_provider_is_400(web):
    r = web.post("/api/admin/ai/providers", json={"name": "x", "kind": "openai"})
    assert r.status_code == 400 and "base URL" in _err(r)["message"]


def test_a_key_and_a_clear_in_one_save_is_400(web):
    r = web.put("/api/admin/ai/providers/1", json={"apiKey": "sk-1", "clearKey": True})
    assert r.status_code == 400 and not web.st["tx"]


def test_an_unknown_provider_is_404(web):
    assert web.put("/api/admin/ai/providers/77", json={"enabled": False}).status_code == 404


def test_the_default_provider_cannot_be_deleted(web):
    r = web.delete("/api/admin/ai/providers/1")
    assert r.status_code == 409 and not web.st["tx"]


def test_a_bad_setting_is_400(web):
    r = web.put("/api/admin/ai", json={"settings": {"effort": "turbo"}})
    assert r.status_code == 400 and not web.st["tx"]


def test_a_good_setting_is_saved_and_the_cache_dropped(web, monkeypatch):
    dropped = []
    monkeypatch.setattr(ai, "save_settings", lambda cur, values, uid, name: list(values))
    monkeypatch.setattr(routes_ai.cfg, "invalidate_cache", lambda: dropped.append(1))
    r = web.put("/api/admin/ai", json={"settings": {"enabled": False}})
    assert r.status_code == 200 and r.json()["applied"] == 1 and dropped


# --- the ai_assisted mark ---------------------------------------------------------


def _target():
    return targets.TargetServer(
        id=7, alias="srv-alpha", host="primary.example.test", port=5432,
        default_database="shop", username="reader", enabled=True, notes=None,
        engine="postgres")


@pytest.fixture
def submit(monkeypatch):
    logging.disable(logging.CRITICAL)
    st = {"ai_on": True}
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": SUPER, "provider": "slack", "sid": "s1", "name": "Ex"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(routes_queries, "_target_by_alias",
                        lambda a: _target() if a == "srv-alpha" else None)
    monkeypatch.setattr(cs.teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ddl"})
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "ddl")
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(cs.targets, "get", lambda tid: _target())
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(cs.auto_approve, "effective_grant", lambda *a, **k: None)
    monkeypatch.setattr(cs.db, "fetch_one", lambda sql, params=None: None)
    from queryhub import lifecycle
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(ai, "is_enabled_for", lambda uid: st["ai_on"])
    reached = {}

    def stop(prep, draft_id=None):
        reached["prep"] = prep
        raise RuntimeError("stop")
    monkeypatch.setattr(routes_queries.core_submit, "create_request", stop)
    c = TestClient(app, raise_server_exceptions=False)
    c.st, c.reached = st, reached
    yield c
    logging.disable(logging.NOTSET)


def _post(c, **body):
    return c.post("/api/queries", json={"connectionId": "srv-alpha", "databaseId": None,
                                        "sql": "SELECT 1", "justification": "j", **body})


@pytest.mark.parametrize("flag,on,want", [(True, True, True), (True, False, False),
                                          (False, True, False)])
def test_the_mark_is_kept_only_while_the_assistant_is_on(submit, flag, on, want):
    submit.st["ai_on"] = on
    _post(submit, aiAssisted=flag)
    assert submit.reached["prep"].ai_assisted is want


def test_both_insert_paths_and_the_returning_list_carry_the_mark():
    src = (ROOT / "src" / "queryhub" / "core_submit.py").read_text()
    assert src.count("query_secret, run_on, ai_assisted)") == 1
    assert src.count(" run_on, \"\n                f\" ai_assisted) \"") == 1
    assert src.count("prep.ai_assisted,\n") == 2
    assert "ai_assisted" in cs.REQUEST_RETURNING.split(",")[-1]
    assert src.count('submit_details["ai_assisted"] = True') == 2


def test_the_slack_card_shows_the_mark():
    row = {"id": 5, "requester_slack_id": PLAIN, "database_name": "shop",
           "query": "SELECT 1", "origin": "web", "wants_result": True,
           "result_format": "csv", "required_tier": "ro"}
    plain = json.dumps(notifications._request_blocks(row, _target(), with_actions=False))
    marked = json.dumps(notifications._request_blocks({**row, "ai_assisted": True}, _target(),
                                                      with_actions=False))
    assert "AI assistant" not in plain and "written with the AI assistant" in marked


def test_history_and_the_queue_carry_the_mark(monkeypatch):
    monkeypatch.setattr(mapping, "pii_preview", lambda query: {"columns": []})
    row = {"id": 5, "query": "SELECT 1", "target_server_id": 7, "database_name": "shop",
           "status": "pending", "created_at": None, "ai_assisted": True,
           "requester_slack_id": PLAIN, "required_tier": "ro", "origin": "web"}
    assert mapping.history_entry(row, lambda tid: "srv-alpha")["aiAssisted"] is True
    assert mapping.queue_item(row, lambda tid: "srv-alpha")["aiAssisted"] is True
    assert mapping.history_entry({**row, "ai_assisted": None},
                                 lambda tid: "srv-alpha")["aiAssisted"] is False


def test_the_migration_and_the_key_rotation_cover_the_new_table():
    mig = (ROOT / "migrations" / "210_ai_assistant.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS ai_providers" in mig
    assert "ADD COLUMN IF NOT EXISTS ai_assisted boolean NOT NULL DEFAULT false" in mig
    assert "('ai_enabled', 'off'" in mig and "ON CONFLICT (key) DO NOTHING" in mig
    rot = (ROOT / "scripts" / "rotate_master_key.py").read_text()
    assert "UPDATE ai_providers SET api_key_encrypted" in rot
    assert "ai_providers.api_key_encrypted" in (ROOT / "docs" / "KEY_ROTATION.md").read_text()


# --- who can use it -------------------------------------------------------------


def test_the_admin_view_carries_the_audience(web):
    body = web.get("/api/admin/ai").json()
    assert body["audience"]["count"] == 3
    assert body["audience"]["rules"][0]["value"] == "super_admin"
    assert "effect" not in body["audience"]["rules"][0]
    assert body["audience"]["roles"]["approver"] == "Approvers"


def test_several_rules_are_added_in_one_call(web, monkeypatch):
    seen = []
    monkeypatch.setattr(ai, "clean_audience", lambda k, v: (k, list(v)))
    monkeypatch.setattr(ai, "add_audience",
                        lambda cur, k, v, uid, name: seen.append((k, v, uid)) or len(v))
    r = web.post("/api/admin/ai/audience", json={"kind": "team", "values": ["4", "5"]})
    assert r.status_code == 201, r.text
    assert seen == [("team", ["4", "5"], SUPER)] and r.json()["added"] == 2
    assert len(web.st["tx"]) == 1


def test_a_bad_audience_write_is_400(web, monkeypatch):
    def bad(*a):
        raise ValueError("Team 9 does not exist.")
    monkeypatch.setattr(ai, "clean_audience", bad)
    r = web.post("/api/admin/ai/audience", json={"kind": "team", "values": ["9"]})
    assert r.status_code == 400 and _err(r)["message"] == "Team 9 does not exist."
    assert not web.st["tx"]


def test_an_audience_rule_is_removed_and_an_unknown_one_is_404(web, monkeypatch):
    gone = []
    monkeypatch.setattr(ai, "remove_audience", lambda cur, rule, uid, name: gone.append(rule["id"]))
    assert web.delete("/api/admin/ai/audience/1").status_code == 200
    assert gone == [1]
    assert web.delete("/api/admin/ai/audience/99").status_code == 404


def test_audience_writes_need_a_super_admin(web):
    web.st["super"] = False
    assert web.post("/api/admin/ai/audience", json={"kind": "everyone"}).status_code == 403
    assert web.delete("/api/admin/ai/audience/1").status_code == 403
    assert not web.st["tx"]


def test_the_audience_migration_seeds_the_super_admins_and_has_no_block():
    mig = (ROOT / "migrations" / "211_ai_audience.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS ai_audience" in mig
    assert "UNIQUE (kind, value)" in mig
    assert "('role', 'super_admin', 'migration 211')" in mig
    assert "ON CONFLICT (kind, value) DO NOTHING" in mig
    assert "effect" not in mig and "block" not in mig.lower()
