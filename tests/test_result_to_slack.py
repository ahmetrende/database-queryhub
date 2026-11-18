"""A requester can ask for the result in Slack too, per request (PLA-2050)."""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from queryhub import bundles, executor
from queryhub import config as cfg
from queryhub.web import routes_queries as rq

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def slack(monkeypatch):
    """Slack on, global key off. Tests change both through the dict."""
    st = {"on": True, "global": False}
    monkeypatch.setattr(cfg, "ENV", SimpleNamespace(slack_enabled=True))

    def set_on(v):
        monkeypatch.setattr(cfg, "ENV", SimpleNamespace(slack_enabled=v))

    st["set_on"] = set_on
    monkeypatch.setattr(cfg, "get_bool",
                        lambda key, default=False: st["global"]
                        if key == "web_result_to_slack" else default)
    return st


# --- the delivery gate -------------------------------------------------------

@pytest.mark.parametrize("origin", ["web", "idp"])
def test_flag_delivers_for_a_non_slack_origin(slack, origin):
    assert executor._deliver_result_to_requester(
        {"origin": origin, "result_to_slack": True}) is True


@pytest.mark.parametrize("origin", ["web", "idp"])
def test_no_flag_does_not_deliver(slack, origin):
    assert executor._deliver_result_to_requester({"origin": origin}) is False
    assert executor._deliver_result_to_requester(
        {"origin": origin, "result_to_slack": False}) is False


def test_global_key_still_delivers(slack):
    slack["global"] = True
    assert executor._deliver_result_to_requester({"origin": "web"}) is True


def test_slack_origin_always_delivers(slack):
    assert executor._deliver_result_to_requester({"origin": "slack"}) is True


def test_slack_disabled_never_delivers(slack):
    slack["set_on"](False)
    slack["global"] = True
    for req in ({"origin": "slack"},
                {"origin": "web", "result_to_slack": True}):
        assert executor._deliver_result_to_requester(req) is False


# --- the API rule: store it only when it can work ---------------------------

def test_a_slack_principal_keeps_the_choice(slack):
    claims = {"sub": "U0EXAMPLE001", "provider": "slack"}
    assert rq._result_to_slack(claims, True) is True
    assert rq._result_to_slack(claims, False) is False
    assert rq.slack_result_available(claims) is True


def test_a_local_principal_stores_false(slack):
    claims = {"sub": "local:alice", "provider": "local"}
    assert rq._result_to_slack(claims, True) is False
    assert rq.slack_result_available(claims) is False


def test_slack_off_stores_false(slack):
    slack["set_on"](False)
    claims = {"sub": "U0EXAMPLE001", "provider": "slack"}
    assert rq._result_to_slack(claims, True) is False
    assert rq.slack_result_available(claims) is False


def test_the_routes_pass_the_choice_on():
    src = (ROOT / "src" / "queryhub" / "web" / "routes_queries.py").read_text()
    assert "resultToSlack: bool = False" in src
    assert src.count("_result_to_slack(claims, body.resultToSlack)") == 2
    assert "slackResultAvailable" in (
        ROOT / "src" / "queryhub" / "web" / "app.py").read_text()


def test_the_insert_paths_write_the_column():
    src = (ROOT / "src" / "queryhub" / "core_submit.py").read_text()
    assert src.count("prep.result_to_slack,\n") == 2
    assert "result_to_slack" in (
        ROOT / "migrations" / "214_request_result_to_slack.sql").read_text()


# --- a bundle stamps every item ---------------------------------------------

class _Cur:
    def __init__(self):
        self.item_params = []
        self._n = 0

    def execute(self, sql, params=None):
        if "INSERT INTO requests" in sql:
            self.item_params.append(params)
            self._n += 1
            self._row = {"id": self._n, "result_to_slack": params[-1]}
        else:
            self._row = {"id": 9, "created_at": None}

    def fetchone(self):
        return self._row


@pytest.mark.parametrize("flag", [True, False])
def test_every_bundle_item_carries_the_flag(flag):
    items = [{"target_server_id": 1, "database_name": "d", "query": f"SELECT {i}",
              "wants_result": True, "explain_plan": None} for i in range(3)]
    cur = _Cur()
    res = bundles.insert_bundle_with_items(
        cur, requester_slack_id="U0EXAMPLE001", requester_name="Ex",
        justification=None, scheduled_for=None, items=items, origin="web",
        result_to_slack=flag)
    assert len(cur.item_params) == 3
    assert all(p[-1] is flag for p in cur.item_params)
    assert all(r["result_to_slack"] is flag for r in res["item_rows"])


# --- over HTTP: the real app, from the body to core_submit and /api/me -------

@pytest.fixture
def web(monkeypatch):
    """A real TestClient. Slack and the principal change through the dict."""
    import logging

    from fastapi.testclient import TestClient

    from queryhub import admins, core_submit, db, requesters, teams
    from queryhub.slack_app import notifications
    from queryhub.web import admin as admin_mod
    from queryhub.web import app as web_app
    from queryhub.web import deps, sessions

    logging.disable(logging.CRITICAL)
    st = {"sub": "U0EXAMPLE001", "provider": "slack", "slack": True, "seen": {}}
    monkeypatch.setattr(type(cfg.ENV), "slack_enabled",
                        property(lambda self: st["slack"]))
    monkeypatch.setattr(deps, "slack_employment", lambda pid: "active")
    monkeypatch.setattr(db, "init_pool", lambda: None)
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "verify_access",
                        lambda t: {"sub": st["sub"], "sid": "s1",
                                   "provider": st["provider"]} if t == "good" else None)
    monkeypatch.setattr(sessions, "session_alive", lambda sid, principal=None: True)
    monkeypatch.setattr(requesters, "is_allowed", lambda uid: True)
    # The submit route needs an approver (no grant lookups). /api/me then needs
    # no admin block, which reads the bot DB.
    monkeypatch.setattr(admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(admin_mod, "admin_block", lambda uid: None)
    target = SimpleNamespace(id=7, alias="svc", enabled=True, engine="postgres",
                             default_database="app")
    monkeypatch.setattr(rq, "_target_by_alias", lambda a: target)
    monkeypatch.setattr(teams, "effective_grant_for_user",
                        lambda uid, tid: {"mode": "ddl"})

    def fake_validate(*a, **kw):
        st["seen"].update(kw)
        return core_submit.Rejection("query", "stopped here for the test")

    monkeypatch.setattr(core_submit, "validate_submission", fake_validate)
    monkeypatch.setattr(rq.core_submit, "validate_submission", fake_validate)
    try:
        with TestClient(web_app.create_app()) as c:
            c.cookies.set("qh_session", "good")
            c.st = st
            yield c
    finally:
        logging.disable(logging.NOTSET)


def _post(c, **extra):
    return c.post("/api/queries", json={"connectionId": "svc", "databaseId": "app",
                                        "sql": "SELECT 1", **extra})


@pytest.mark.parametrize("asked", [True, False])
def test_the_body_reaches_core_submit(web, asked):
    _post(web, resultToSlack=asked)
    assert web.st["seen"]["result_to_slack"] is asked


def test_an_older_client_that_omits_it_gets_false(web):
    _post(web)
    assert web.st["seen"]["result_to_slack"] is False


def test_slack_off_drops_the_choice_over_http(web):
    web.st["slack"] = False
    _post(web, resultToSlack=True)
    assert web.st["seen"]["result_to_slack"] is False


@pytest.mark.parametrize("slack_on", [True, False])
def test_me_says_whether_the_checkbox_can_work(web, slack_on):
    web.st["slack"] = slack_on
    body = web.get("/api/me").json()
    assert body["user"]["slackResultAvailable"] is slack_on


# --- the classic UI ----------------------------------------------------------

def test_the_box_shows_only_when_slack_can_reach_the_person():
    src = (ROOT / "QueryHubWeb" / "qh-app.jsx").read_text()
    # The choice is sent only when /api/me allows it and the box is ticked.
    assert "const toSlackOn = !!(user && user.slackResultAvailable) && toSlack;" in src
    assert src.count("...(toSlackOn ? { resultToSlack: true } : null)") == 2
    # The box itself renders only behind the same flag. Off by default.
    assert "{slackResult && (" in src
    assert "slackResult={!!(user && user.slackResultAvailable)}" in src
    assert "localStorage.getItem('qh.resultToSlack.v1') === '1'" in src
