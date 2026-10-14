"""The IdP panel's QueryHub routes work without the slack_app package (PLA-1410).

The panel calls these routes:

- `POST /api/endpoint-requests`: a person asks for access.
- `GET /api/auto-approve-requests`: their windows, and the lengths.
- `POST /api/auto-approve-requests`: a person asks for a window.
- `GET /api/admin/auto-approve-requests`: the windows that wait.
- `POST /api/admin/auto-approve-requests/{id}/decision`: an admin decides a window.
- `POST /api/admin/endpoint-requests/{id}/decision`: an admin decides access.

Slack stays next to the panel. But these routes must not need it: with Slack
off, and also when no `queryhub.slack_app` module can be imported. These
tests drive each route through the real app (TestClient). Only the database,
the identity and Slack are fakes. They check:

1. With every slack_app module blocked, each route answers 2xx. It writes the
   outbox row of its event while `idp_outbox_enabled` is on. The status, the
   body, the outbox row and the SQL are the same as with Slack off and
   slack_app present.
2. With Slack on, each route sends the same Slack calls, with the same
   arguments, as before PLA-1410.
3. In a fresh interpreter with slack_app blocked from the start, the web app
   and the modules of these routes import. The in-process tests cannot see a
   module-level import, because an earlier test already imported the module.
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
import os
import pkgutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import queryhub
from queryhub import access_requests, admins, grants, idp_outbox, targets, teams
from queryhub import auto_approve_requests as aar
from queryhub import config as cfg
from queryhub import db
from queryhub.web import app as web_app
from queryhub.web import deps, routes_queries
from queryhub.web import routes_requests as rr

SLACK_PACKAGE = "queryhub.slack_app"
REQUESTER = "U0REQUESTER"
ADMIN = "U0ADMIN0001"
REQUESTER_CLAIMS = {"sub": REQUESTER, "name": "Dev One", "provider": "idp", "sid": None}
ADMIN_CLAIMS = {"sub": ADMIN, "name": "Admin One", "provider": "idp", "sid": None}
TARGET = targets.TargetServer(
    id=21, alias="demo-orders", host="h.example", port=5432,
    default_database="demo_db", username="u", enabled=True, notes=None,
    engine="postgres")

ACCESS_PENDING = idp_outbox.ACCESS_REQUEST_PENDING
ACCESS_DECIDED = idp_outbox.ACCESS_REQUEST_DECIDED
WINDOW_PENDING = idp_outbox.WINDOW_REQUEST_PENDING
WINDOW_DECIDED = idp_outbox.WINDOW_REQUEST_DECIDED


def _slack_module_names() -> list[str]:
    """The package and each module in it, read from the directory. So the
    block also stops a slack_app module that a later change adds."""
    pkg_dir = Path(queryhub.__file__).parent / "slack_app"
    return [SLACK_PACKAGE] + sorted(f"{SLACK_PACKAGE}.{m.name}"
                                    for m in pkgutil.iter_modules([str(pkg_dir)]))


def _block_slack_app(monkeypatch) -> None:
    """From now on, every slack_app import fails. `None` in sys.modules stops
    the import, also for a module that an earlier test imported."""
    for name in _slack_module_names():
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(ImportError):
        importlib.import_module(f"{SLACK_PACKAGE}.access")


@pytest.fixture
def no_slack_app(monkeypatch):
    _block_slack_app(monkeypatch)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class FakeSlack:
    """Records, in order, every Slack API call."""

    def __init__(self, calls: list):
        self.calls = calls

    def conversations_open(self, users):
        self.calls.append(("conversations_open", {"users": users}))
        return {"channel": {"id": f"D-{users}"}}

    def chat_postMessage(self, **kw):
        self.calls.append(("chat_postMessage", kw))
        return {"ts": f"{len(self.calls)}.000", "channel": kw.get("channel")}

    def chat_update(self, **kw):
        self.calls.append(("chat_update", kw))
        return {"ok": True}

    def files_upload_v2(self, **kw):
        self.calls.append(("files_upload_v2", kw))
        return {"ok": True}


class World:
    """Every fact that the fakes read, and every write that they record."""

    def __init__(self):
        self.outbox: list[tuple[str, list[str], dict]] = []
        self.sql: list[tuple[str, object]] = []
        self.slack: list[tuple[str, dict]] = []
        self.created: list[dict] = []
        self.list_active_calls = 0
        self.claims = REQUESTER_CLAIMS
        self.access_row = {
            "id": 12, "requester_slack_id": REQUESTER, "requester_name": "Dev One",
            "target_server_id": 21, "database_name": "demo_db",
            "attempted_query": None, "reason": "[requested tier: RO] need one look",
            "requested_tier": "ro", "status": "pending",
            "decided_by_slack_id": None, "decided_by_name": None,
            "decision_reason": None, "created_at": None, "decided_at": None}
        self.window_row = {
            "id": 7, "requester_slack_id": REQUESTER, "requester_name": "Dev One",
            "target_server_id": 21, "database_name": None, "max_tier": "ro",
            "window_minutes": 1440, "reason": "reading the ledger",
            "status": "pending", "decided_by_slack_id": None,
            "decided_by_name": None, "decided_at": None, "granted_id": None,
            "created_at": None}
        # GET routes list these: a Slack window, day windows, an odd length.
        self.listed = [dict(self.window_row, id=i, alias="demo-orders", window_minutes=m)
                       for i, m in ((7, 480), (8, 10080), (9, 1440), (10, 90))]


class FakeTx:
    """What `db.transaction()` yields. It answers an UPDATE ... RETURNING the
    way Postgres would, from the pending row. It finds no grant yet."""

    def __init__(self, w: World):
        self.w, self._next = w, None

    def execute(self, sql, params=None):
        sql = _norm(sql)
        self.w.sql.append((sql, params))
        self._next = None
        if sql.startswith("UPDATE access_requests"):
            status, by_id, by_name, note, _rid = params
            self._next = dict(self.w.access_row, status=status,
                              decided_by_slack_id=by_id, decided_by_name=by_name,
                              decision_reason=note)
        elif sql.startswith("UPDATE auto_approve_requests"):
            self._next = dict(self.w.window_row, status=params[0])
        return self

    def fetchone(self):
        return self._next


class FakeCur:
    """The cursor of `db.connection()`, for the window approval."""

    def __init__(self, w: World):
        self.w, self.rowcount = w, 1

    def execute(self, sql, params=None):
        self.w.sql.append((_norm(sql), params))

    def fetchone(self):
        return {"id": 99}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, w: World):
        self.w = w

    def cursor(self):
        return FakeCur(self.w)

    def commit(self):
        self.w.sql.append(("COMMIT", None))

    def rollback(self):
        self.w.sql.append(("ROLLBACK", None))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def world(monkeypatch):
    """The fakes, with Slack off and `idp_outbox_enabled` on."""
    w = World()

    def get_setting(key, default=None):
        if key == "idp_outbox_enabled":
            return "on"
        if default is None:
            raise KeyError(f"bot_config key not set in test: {key}")
        return default
    monkeypatch.setattr(cfg, "get_setting", get_setting)
    # Slack off: no bot token, so the real `_bot_client()` answers None.
    monkeypatch.setattr(cfg, "ENV", dataclasses.replace(cfg.ENV, slack_bot_token=""))

    def execute(sql, params=None):
        sql = _norm(sql)
        if "notification_outbox" in sql:
            event_type, recipients, payload = params[0], params[1], json.loads(params[2])
            w.outbox.append((event_type, recipients, payload))
        else:
            w.sql.append((sql, params))
    monkeypatch.setattr(db, "execute", execute)

    @contextlib.contextmanager
    def transaction():
        yield FakeTx(w)
    monkeypatch.setattr(db, "transaction", transaction)
    monkeypatch.setattr(db, "connection", lambda: FakeConn(w))

    # Each call answers with a longer list. So the outbox row and the DMs name
    # the same people only when they come from one call.
    def list_active():
        w.list_active_calls += 1
        return [{"slack_user_id": f"U0ADMIN000{i}", "name": f"Admin {i}",
                 "source": "permanent", "expires_at": None}
                for i in range(1, w.list_active_calls + 2)]
    monkeypatch.setattr(admins, "list_active", list_active)
    monkeypatch.setattr(admins, "is_admin", lambda pid: pid.startswith("U0ADMIN"))
    monkeypatch.setattr(admins, "is_super_admin", lambda pid: pid.startswith("U0ADMIN"))
    monkeypatch.setattr(admins, "can_approve", lambda pid, req: True)
    monkeypatch.setattr(grants, "control_plane_target_ids", lambda: set())
    monkeypatch.setattr(targets, "get", lambda tid: TARGET if tid == 21 else None)
    monkeypatch.setattr(targets, "by_alias", lambda a: TARGET if a == "demo-orders" else None)
    monkeypatch.setattr(teams, "can_use_target", lambda pid, tid: True)
    monkeypatch.setattr(deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rr, "_target_by_alias", lambda a: TARGET if a == "demo-orders" else None)

    def create(principal_id, name, target_server_id, database_name,
               attempted_query, reason, requested_tier=None):
        row = dict(w.access_row, requester_slack_id=principal_id,
                   requester_name=name, target_server_id=target_server_id,
                   database_name=database_name, attempted_query=attempted_query,
                   reason=reason, requested_tier=requested_tier, status="pending")
        w.created.append(row)
        return row
    monkeypatch.setattr(access_requests, "create", create)
    monkeypatch.setattr(access_requests, "open_count_for", lambda pid: 0)
    monkeypatch.setattr(access_requests, "get",
                        lambda rid: dict(w.access_row) if rid == 12 else None)
    monkeypatch.setattr(access_requests, "list_admin_dms",
                        lambda rid: [{"channel_id": "D-U0ADMIN0001", "message_ts": "1.000"},
                                     {"channel_id": "D-U0ADMIN0002", "message_ts": "2.000"}])

    # The real `submit_window` runs: it checks the window length, and that
    # check is one of the places that imported slack_app.
    def window_create(**kw):
        row = dict(w.window_row, window_minutes=kw["window_minutes"],
                   max_tier=kw["max_tier"], database_name=kw["database_name"])
        w.created.append(row)
        return row
    monkeypatch.setattr(aar, "create", window_create)
    monkeypatch.setattr(aar, "find_pending_for", lambda pid, tid: None)
    monkeypatch.setattr(aar, "get", lambda rid: dict(w.window_row) if rid == 7 else None)
    monkeypatch.setattr(aar, "list_for", lambda pid, limit=50: w.listed)
    monkeypatch.setattr(aar, "list_pending", lambda limit=200: w.listed)
    return w


def _slack_on(w: World, monkeypatch) -> None:
    """Slack configured, with a client that records every call."""
    monkeypatch.setattr(cfg, "ENV", dataclasses.replace(cfg.ENV, slack_bot_token="xoxb-test"))
    client = FakeSlack(w.slack)
    monkeypatch.setattr(rr, "_bot_client", lambda: client)
    monkeypatch.setattr(routes_queries, "_bot_client", lambda: client)


def _call(w: World, claims: dict, method: str, path: str, body):
    """One request through the real app. The identity is the only dependency
    that changes."""
    w.claims = claims
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: dict(w.claims)
    return TestClient(app).request(method, path, json=body)


# ---- the six routes -------------------------------------------------------------

ROUTES = [
    pytest.param(REQUESTER_CLAIMS, "POST", "/api/endpoint-requests",
                 {"server": "demo-orders", "database": "demo_db", "tier": "RO",
                  "reason": "need read access"},
                 201, ACCESS_PENDING, id="access-request"),
    pytest.param(REQUESTER_CLAIMS, "POST", "/api/endpoint-requests",
                 {"server": "new-cluster", "database": "demo_db", "tier": "RW",
                  "reason": "need read access"},
                 201, ACCESS_PENDING, id="access-request-free-text"),
    pytest.param(REQUESTER_CLAIMS, "GET", "/api/auto-approve-requests", None,
                 200, None, id="my-windows"),
    pytest.param(REQUESTER_CLAIMS, "POST", "/api/auto-approve-requests",
                 {"connectionId": "demo-orders", "days": 1, "reason": "reading the ledger"},
                 201, WINDOW_PENDING, id="window-request-days"),
    pytest.param(REQUESTER_CLAIMS, "POST", "/api/auto-approve-requests",
                 {"connectionId": "demo-orders", "windowMinutes": 480,
                  "reason": "reading the ledger"},
                 201, WINDOW_PENDING, id="window-request-hours"),
    pytest.param(ADMIN_CLAIMS, "GET", "/api/admin/auto-approve-requests", None,
                 200, None, id="admin-windows"),
    pytest.param(ADMIN_CLAIMS, "POST", "/api/admin/auto-approve-requests/7/decision",
                 {"approve": True}, 200, WINDOW_DECIDED, id="window-approve"),
    pytest.param(ADMIN_CLAIMS, "POST", "/api/admin/auto-approve-requests/7/decision",
                 {"approve": False}, 200, WINDOW_DECIDED, id="window-reject"),
    pytest.param(ADMIN_CLAIMS, "POST", "/api/admin/endpoint-requests/12/decision",
                 {"approve": True}, 200, ACCESS_DECIDED, id="access-approve"),
    pytest.param(ADMIN_CLAIMS, "POST", "/api/admin/endpoint-requests/12/decision",
                 {"approve": False, "note": "too broad"}, 200, ACCESS_DECIDED,
                 id="access-reject"),
]


@pytest.mark.parametrize("claims,method,path,body,status,event", ROUTES)
def test_without_slack_app_each_route_answers_as_with_slack_off(
        world, monkeypatch, caplog, claims, method, path, body, status, event):
    with_package = _call(world, claims, method, path, body)
    seen_with_package = (with_package.json(), world.outbox, world.sql)
    world.outbox, world.sql, world.list_active_calls = [], [], 0

    _block_slack_app(monkeypatch)
    caplog.clear()
    without_package = _call(world, claims, method, path, body)

    assert without_package.status_code == status, without_package.text
    assert [e for e, _r, _p in world.outbox] == ([event] if event else [])
    # idp_outbox swallows a failure, so a lost row shows only in the log.
    assert "notification_outbox row" not in caplog.text
    assert world.slack == []
    assert with_package.status_code == status
    assert (without_package.json(), world.outbox, world.sql) == seen_with_package


def test_the_window_lengths_and_labels_need_no_slack_app(world, no_slack_app):
    mine = _call(world, REQUESTER_CLAIMS, "GET", "/api/auto-approve-requests", None)
    pending = _call(world, ADMIN_CLAIMS, "GET", "/api/admin/auto-approve-requests", None)

    assert mine.status_code == 200 and pending.status_code == 200
    assert mine.json()["windowOptions"] == [
        {"minutes": 60, "label": "1 hour"},
        {"minutes": 180, "label": "3 hours"},
        {"minutes": 480, "label": "8 hours"},
        {"minutes": 1440, "label": "1 day", "days": 1},
        {"minutes": 10080, "label": "7 days", "days": 7},
        {"minutes": 20160, "label": "14 days", "days": 14},
        {"minutes": 43200, "label": "30 days", "days": 30}]
    assert [r["windowLabel"] for r in pending.json()["requests"]] == [
        "8h", "7 days", "1 day", "90 min"]
    # The window check of a new request, and the labels of the outbox rows.
    assert aar.valid_window(480) and aar.valid_window(7 * 1440)
    assert not aar.valid_window(90)
    assert [idp_outbox._window_label(m) for m in (480, 10080, None)] == ["8h", "7 days", ""]


def test_without_slack_app_the_outbox_rows_keep_their_window_label(world, no_slack_app):
    _call(world, REQUESTER_CLAIMS, "POST", "/api/auto-approve-requests",
          {"connectionId": "demo-orders", "days": 7, "reason": "reading the ledger"})
    _call(world, ADMIN_CLAIMS, "POST", "/api/admin/auto-approve-requests/7/decision",
          {"approve": True})

    assert [(e, p["windowLabel"]) for e, _r, p in world.outbox] == [
        (WINDOW_PENDING, "7 days"), (WINDOW_DECIDED, "1 day")]
    # A pending row names the admins from the fan-out's own list.
    assert world.outbox[0][1] == ["U0ADMIN0001", "U0ADMIN0002"]


# ---- with Slack on: the same Slack calls as before ------------------------------

def _with_divider(blocks: list) -> list:
    """`notifications._post` and `_update` end every card with a divider."""
    return blocks + [{"type": "divider"}]


def _dm(user: str, text: str) -> list:
    """What `notifications.dm_requester` sends for a plain text."""
    section = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    return [("conversations_open", {"users": user}),
            ("chat_postMessage", {"channel": f"D-{user}", "text": text,
                                  "blocks": _with_divider([section])})]


@pytest.mark.parametrize("server,requested", [
    pytest.param("demo-orders", "demo-orders", id="known-target"),
    pytest.param("new-cluster", "new-cluster", id="free-text-server"),
])
def test_with_slack_on_an_access_request_sends_the_same_admin_dms(
        world, monkeypatch, server, requested):
    from queryhub.slack_app import access
    _slack_on(world, monkeypatch)

    r = _call(world, REQUESTER_CLAIMS, "POST", "/api/endpoint-requests",
              {"server": server, "database": "demo_db", "tier": "RO",
               "reason": "need read access"})

    assert r.status_code == 201, r.text
    row = world.created[-1]
    target = TARGET if server == "demo-orders" else None
    card = _with_divider(access.admin_dm_blocks(row, target, requested_server=requested))
    text = f"Access request from <@{REQUESTER}> for " + (
        "demo-orders" if target else "an unlisted server") + " — request #12"
    admins_dmed = ["U0ADMIN0001", "U0ADMIN0002"]
    assert world.slack == [call for adm in admins_dmed for call in (
        ("conversations_open", {"users": adm}),
        ("chat_postMessage", {"channel": f"D-{adm}", "blocks": card, "text": text}))]
    # QueryHub records each DM for the lockstep update on decision.
    assert [p for s, p in world.sql if "access_request_notifications" in s] == [
        (12, "U0ADMIN0001", "D-U0ADMIN0001", "2.000"),
        (12, "U0ADMIN0002", "D-U0ADMIN0002", "4.000")]
    assert r.json()["slackMessageTs"] == "2.000"
    # The outbox row names the admins that got a DM, from the same list.
    assert world.outbox[0][:2] == (ACCESS_PENDING, admins_dmed)


def test_with_slack_on_a_window_request_sends_the_same_admin_dms(world, monkeypatch):
    from queryhub.slack_app import ro_window
    _slack_on(world, monkeypatch)

    r = _call(world, REQUESTER_CLAIMS, "POST", "/api/auto-approve-requests",
              {"connectionId": "demo-orders", "days": 1, "reason": "reading the ledger"})

    assert r.status_code == 201, r.text
    assert r.json()["adminsNotified"] == 2
    card = _with_divider(ro_window.admin_dm_blocks(world.created[-1], "demo-orders"))
    text = f"RO auto-approve window request from <@{REQUESTER}> for demo-orders (#7)"
    admins_dmed = ["U0ADMIN0001", "U0ADMIN0002"]
    assert world.slack == [call for adm in admins_dmed for call in (
        ("conversations_open", {"users": adm}),
        ("chat_postMessage", {"channel": f"D-{adm}", "text": text, "blocks": card}))]
    assert world.outbox[0][:2] == (WINDOW_PENDING, admins_dmed)


@pytest.mark.parametrize("approve,text", [
    pytest.param(True, ":zap: Your *RO* auto-approve window on `demo-orders` is active "
                       "for the next 1 day. Matching queries skip approval and dispatch "
                       "immediately.", id="approve"),
    pytest.param(False, ":no_entry: An admin declined your auto-approve window request (#7).",
                 id="reject"),
])
def test_with_slack_on_a_window_decision_sends_the_same_dm(world, monkeypatch, approve, text):
    _slack_on(world, monkeypatch)

    r = _call(world, ADMIN_CLAIMS, "POST", "/api/admin/auto-approve-requests/7/decision",
              {"approve": approve})

    assert r.status_code == 200, r.text
    assert world.slack == _dm(REQUESTER, text)


@pytest.mark.parametrize("body,status_line,requester_dm", [
    pytest.param(
        {"approve": True},
        f":white_check_mark: Approved by <@{ADMIN}>\n"
        ":key: Granted automatically: *RO* on `demo_db`.",
        f":white_check_mark: *Access request `#12` approved* by <@{ADMIN}>.\n"
        "*Target:* `demo-orders`  •  *Database:* `demo_db`\n\n"
        "You can now run `/sql` — your access is active.",
        id="approve"),
    pytest.param(
        {"approve": False, "note": "too broad"},
        f":x: Rejected by <@{ADMIN}> — too broad",
        f":x: *Access request `#12` rejected* by <@{ADMIN}>\n"
        "*Target:* `demo-orders`  •  *Database:* `demo_db`\n*Reason:* too broad",
        id="reject"),
])
def test_with_slack_on_an_access_decision_sends_the_same_slack_calls(
        world, monkeypatch, body, status_line, requester_dm):
    from queryhub.slack_app import access
    _slack_on(world, monkeypatch)

    r = _call(world, ADMIN_CLAIMS, "POST", "/api/admin/endpoint-requests/12/decision", body)

    assert r.status_code == 200, r.text
    # Every admin's card loses its buttons. Then QueryHub tells the requester.
    card = _with_divider(access.resolved_admin_dm_blocks(world.access_row, TARGET,
                                                         status_line))
    assert world.slack == [
        ("chat_update", {"channel": channel, "ts": ts, "blocks": card, "text": status_line})
        for channel, ts in (("D-U0ADMIN0001", "1.000"), ("D-U0ADMIN0002", "2.000"))
    ] + _dm(REQUESTER, requester_dm)


def test_the_slack_modal_fan_out_sends_the_same_dms_and_snippets(world, monkeypatch):
    """The Slack modal reaches the fan-out through `fan_out_admin_dms`. A query
    longer than the inline limit goes to each admin as a thread snippet."""
    from queryhub.slack_app import access, handlers
    _slack_on(world, monkeypatch)
    monkeypatch.setattr(handlers, "_kill_switch_on", lambda: False)
    monkeypatch.setattr(access_requests, "find_pending_for", lambda *a: None)
    long_sql = "SELECT " + ", ".join(f"col_{i}" for i in range(120)) + " FROM demo"
    values = {
        access.B_TARGET: {access.A_TARGET: {"selected_option": {"value": "21"}}},
        access.B_DATABASE: {access.A_DATABASE: {"value": "demo_db"}},
        access.B_QUERY: {access.A_QUERY: {"value": long_sql}},
        access.B_REASON: {access.A_REASON: {"value": "need read access"}},
    }
    body = {"user": {"id": REQUESTER, "name": "Dev One"},
            "view": {"state": {"values": values}}}

    handlers.handle_access_request_submission(lambda *a, **k: None, body, FakeSlack(world.slack))

    row = world.created[-1]
    assert row["attempted_query"] == long_sql and len(long_sql) > 500
    card = _with_divider(access.admin_dm_blocks(row, TARGET))
    text = f"Access request from <@{REQUESTER}> for demo-orders — request #12"
    # The handler reads the admin list once to check for an admin. The
    # fan-out reads it again, so its list (three admins) names the DMs.
    admins_dmed = ["U0ADMIN0001", "U0ADMIN0002", "U0ADMIN0003"]
    expected = []
    for adm in admins_dmed:
        ts = f"{len(expected) + 2}.000"
        expected += [
            ("conversations_open", {"users": adm}),
            ("chat_postMessage", {"channel": f"D-{adm}", "blocks": card, "text": text}),
            ("files_upload_v2", {"channel": f"D-{adm}", "content": long_sql,
                                 "filename": "request_12.sql",
                                 "title": "SQL for request #12",
                                 "snippet_type": "sql", "thread_ts": ts})]
    expected += _dm(REQUESTER, ":hourglass_flowing_sand: *Access request `#12` submitted* "
                               "— admins will review.\n"
                               "*Target:* `demo-orders`  •  *Database:* `demo_db`")
    assert world.slack == expected
    assert world.outbox[0][:2] == (ACCESS_PENDING, admins_dmed)


# ---- a module-level import -------------------------------------------------------

def test_the_web_app_and_the_route_modules_import_without_slack_app():
    """Block slack_app before the first import of the package. Then build the
    web app, and import the modules that the six routes use. A module-level
    slack_app import on that path stops the import here."""
    code = textwrap.dedent(f"""
        import sys
        for name in {_slack_module_names()!r}:
            sys.modules[name] = None
        from queryhub.web import app
        app.create_app()
        import queryhub.access_requests, queryhub.auto_approve_requests
        import queryhub.idp_outbox, queryhub.window_options
        import queryhub.web.routes_admin, queryhub.web.routes_requests
        loaded = sorted(n for n, m in sys.modules.items()
                        if n.startswith("{SLACK_PACKAGE}") and m is not None)
        print("slack_app modules loaded:", loaded)
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=dict(os.environ), timeout=120)

    assert out.returncode == 0, out.stderr
    assert "slack_app modules loaded: []" in out.stdout
