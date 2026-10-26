"""The IdP panel's outbox events for access requests and auto-approve windows.

`idp_outbox` writes four events (PLA-1408):

    queryhub.access_request_pending    queryhub.access_request_decided
    queryhub.window_request_pending    queryhub.window_request_decided

Each request kind has a Slack path and a web path. These tests drive the real
entry points of both (the Slack handlers and the web routes). Only the
database and Slack are recorders. For every path they check:

1. With `idp_outbox_enabled` on, the path writes exactly one row of its event.
2. A pending event names the admins that the Slack fan-out DMed, from the same
   list. A decided event names the requester.
3. Every payload value is a string (Ruling P-10).
4. With the switch off, the path writes no row.
5. The outbox changes nothing that the caller, Slack or the other tables see.
   This is also true when the insert fails.

The web paths also run with Slack off, because the rows exist for that case.
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import access_grant_default, access_requests, admins, grants, idp_outbox, targets
from queryhub import auto_approve_requests as aar
from queryhub import config as cfg
from queryhub import db
from queryhub.slack_app import access, handlers, ro_window
from queryhub.web import deps, routes_queries
from queryhub.web import routes_admin as ra
from queryhub.web import routes_requests as rr

REQUESTER = "U0REQUESTER"
ADMIN = "U0ADMIN1"
TARGET = targets.TargetServer(
    id=21, alias="demo-orders", host="h.example", port=5432,
    default_database="demo_db", username="u", enabled=True, notes=None,
    engine="postgres")


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class FakeSlack:
    """Records, in order, every DM that QueryHub opens and every message that
    it posts or edits."""

    def __init__(self):
        self.opened: list[str] = []
        self.sent: list[tuple] = []

    def conversations_open(self, users):
        self.opened.append(users)
        return {"channel": {"id": f"D-{users}"}}

    def chat_postMessage(self, **kw):
        self.sent.append(("post", kw.get("channel"), kw.get("text"),
                          json.dumps(kw.get("blocks"), sort_keys=True, default=str)))
        return {"ts": f"{len(self.sent)}.000", "channel": kw.get("channel")}

    def chat_update(self, **kw):
        self.sent.append(("update", kw.get("channel"), kw.get("text"),
                          json.dumps(kw.get("blocks"), sort_keys=True, default=str)))
        return {"ok": True}


class World:
    """Every fact the fakes read, and every write they record."""

    def __init__(self):
        self.flag = "on"              # bot_config idp_outbox_enabled
        self.flag_error = False       # the bot_config read raises
        self.fail_insert = False      # the notification_outbox insert raises
        self.race = False             # another admin decided first
        self.env = SimpleNamespace(slack_enabled=True)
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
        self.reset()

    def reset(self):
        self.rows: list = []          # (sql, params) of each outbox insert
        self.sql: list = []           # every other statement, in order
        self.created: list = []       # the rows that a fake create built
        self.list_active_calls = 0
        self.slack = FakeSlack()

    def client(self):
        return self.slack if self.env.slack_enabled else None

    def payloads(self) -> list[dict]:
        return [json.loads(params[2]) for _, params in self.rows]


class FakeTx:
    """What `db.transaction()` yields. It answers an UPDATE ... RETURNING the
    way Postgres would, from the pending row. It finds no grant yet."""

    def __init__(self, w: World):
        self.w, self._next = w, None

    def execute(self, sql, params=None):
        sql = _norm(sql)
        self.w.sql.append((sql, params))
        self._next = None
        if sql.startswith("UPDATE access_requests") and not self.w.race:
            status, by_id, by_name, note, _rid = params
            self._next = dict(self.w.access_row, status=status,
                              decided_by_slack_id=by_id, decided_by_name=by_name,
                              decision_reason=note)
        elif sql.startswith("UPDATE auto_approve_requests") and not self.w.race:
            self._next = dict(self.w.window_row, status=params[0])
        return self

    def fetchone(self):
        return self._next


class FakeCur:
    """The cursor of `db.connection()`, for the window approval."""

    def __init__(self, w: World):
        self.w, self.rowcount = w, 1

    def execute(self, sql, params=None):
        sql = _norm(sql)
        self.w.sql.append((sql, params))
        if sql.startswith("UPDATE auto_approve_requests"):
            self.rowcount = 0 if self.w.race else 1

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
def w(monkeypatch):
    w = World()
    real_get_setting = cfg.get_setting

    def get_setting(key, default=None):
        if key == "idp_outbox_enabled":
            if w.flag_error:
                raise RuntimeError("bot_config is unreachable")
            return w.flag
        return real_get_setting(key, default)
    monkeypatch.setattr(cfg, "get_setting", get_setting)
    monkeypatch.setattr(cfg, "ENV", w.env)
    # The end of a grant that an approval writes is "now plus the default days".
    # Three runs of one decision must compare equal, so the end is fixed.
    monkeypatch.setattr(access_grant_default, "approval_end",
                        lambda *a, **k: datetime(2027, 1, 1, tzinfo=timezone.utc))

    def execute(sql, params=None):
        sql = _norm(sql)
        if "notification_outbox" in sql:
            if w.fail_insert:
                raise RuntimeError("the outbox insert failed")
            w.rows.append((sql, params))
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
        return [{"slack_user_id": f"U0ADMIN{i}", "name": f"Admin {i}",
                 "source": "permanent", "expires_at": None}
                for i in range(1, w.list_active_calls + 2)]
    monkeypatch.setattr(admins, "list_active", list_active)
    monkeypatch.setattr(admins, "is_admin", lambda pid: pid.startswith("U0ADMIN"))
    monkeypatch.setattr(admins, "can_approve", lambda pid, req: True)
    monkeypatch.setattr(grants, "control_plane_target_ids", lambda: set())
    monkeypatch.setattr(targets, "get", lambda tid: TARGET if tid == 21 else None)
    monkeypatch.setattr(targets, "by_alias",
                        lambda a: TARGET if a == "demo-orders" else None)

    def create(principal_id, name, target_server_id, database_name,
               attempted_query, reason, requested_tier=None):
        row = dict(w.access_row, requester_slack_id=principal_id,
                   requester_name=name, target_server_id=target_server_id,
                   database_name=database_name, attempted_query=attempted_query,
                   reason=reason, requested_tier=requested_tier, status="pending")
        w.created.append(row)
        return row
    monkeypatch.setattr(access_requests, "create", create)
    monkeypatch.setattr(access_requests, "find_pending_for", lambda *a: None)
    monkeypatch.setattr(access_requests, "open_count_for", lambda pid: 0)
    monkeypatch.setattr(access_requests, "get",
                        lambda rid: dict(w.access_row) if rid == w.access_row["id"] else None)
    monkeypatch.setattr(access_requests, "list_admin_dms",
                        lambda rid: [{"channel_id": f"D-{ADMIN}", "message_ts": "1.000"}])

    def submit_window(**kw):
        row = dict(w.window_row, window_minutes=kw["window_minutes"], status="pending")
        w.created.append(row)
        return row, TARGET
    monkeypatch.setattr(aar, "submit_window", submit_window)
    monkeypatch.setattr(aar, "get",
                        lambda rid: dict(w.window_row) if rid == w.window_row["id"] else None)

    monkeypatch.setattr(deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rr, "_target_by_alias",
                        lambda a: TARGET if a == "demo-orders" else None)
    monkeypatch.setattr(rr.access_requests, "list_visible", lambda: True)
    monkeypatch.setattr(rr.access_requests, "resolve_typed_server",
                        lambda a: TARGET if a == "demo-orders" else None)
    monkeypatch.setattr(rr, "_bot_client", w.client)
    monkeypatch.setattr(routes_queries, "_bot_client", w.client)
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need, **kw: claims["sub"])
    monkeypatch.setattr(handlers, "_kill_switch_on", lambda: False)
    monkeypatch.setattr(handlers, "_guard_admin", lambda *a, **k: True)
    monkeypatch.setattr(handlers, "_admin_in_scope", lambda *a, **k: True)
    return w


# ---- the twelve paths ---------------------------------------------------------

def _ack(*_a, **_k):
    return None


ADMIN_CLAIMS = {"sub": ADMIN, "name": "Admin One"}
SLACK_ADMIN = {"id": ADMIN, "name": "admin.one"}


def web_access_create(w):
    body = rr.EndpointRequestIn(server="demo-orders", database="demo_db",
                                tier="RO", reason="need read access")
    return rr.endpoint_request(body, claims={"sub": REQUESTER, "name": "Dev One"})


def slack_access_create(w):
    values = {
        access.B_TARGET: {access.A_TARGET: {"selected_option": {"value": "21"}}},
        access.B_DATABASE: {access.A_DATABASE: {"value": "demo_db"}},
        access.B_QUERY: {access.A_QUERY: {"value": None}},
        access.B_REASON: {access.A_REASON: {"value": "need read access"}},
    }
    body = {"user": {"id": REQUESTER, "name": "Dev One"},
            "view": {"state": {"values": values}}}
    return handlers.handle_access_request_submission(_ack, body, w.client())


def web_access_approve(w):
    return ra.admin_decide_endpoint(12, ra.EndpointDecisionIn(approve=True), ADMIN_CLAIMS)


def web_access_reject(w):
    return ra.admin_decide_endpoint(
        12, ra.EndpointDecisionIn(approve=False, note="too broad"), ADMIN_CLAIMS)


def slack_access_approve(w):
    body = {"user": SLACK_ADMIN, "actions": [{"value": "12"}]}
    return handlers.handle_access_approve(_ack, body, w.client())


def slack_access_reject(w):
    body = {"user": SLACK_ADMIN, "view": {
        "private_metadata": "12",
        "state": {"values": {"reason_block": {"reason_input": {"value": "too broad"}}}}}}
    return handlers.handle_access_reject_submission(_ack, body, w.client())


def web_window_create(w):
    body = rr.WindowRequestIn(connectionId="demo-orders", days=1,
                              reason="reading the ledger")
    return rr.create_window_request(body, claims={"sub": REQUESTER, "name": "Dev One"})


def slack_window_create(w):
    values = {
        ro_window.B_TARGET: {ro_window.A_TARGET: {"selected_option": {"value": "21"}}},
        ro_window.B_WINDOW: {ro_window.A_WINDOW: {"selected_option": {"value": "480"}}},
        ro_window.B_REASON: {ro_window.A_REASON: {"value": "reading the ledger"}},
    }
    body = {"user": {"id": REQUESTER, "name": "Dev One"},
            "view": {"state": {"values": values}}}
    return handlers.handle_ro_window_submission(_ack, body, w.client())


def web_window_approve(w):
    return ra.admin_decide_window_request(7, ra.WindowDecisionIn(approve=True), ADMIN_CLAIMS)


def web_window_reject(w):
    return ra.admin_decide_window_request(7, ra.WindowDecisionIn(approve=False), ADMIN_CLAIMS)


_WINDOW_CARD = {"user": SLACK_ADMIN, "actions": [{"value": "7"}],
                "container": {"channel_id": f"D-{ADMIN}", "message_ts": "1.000"}}


def slack_window_approve(w):
    return handlers.handle_ro_window_approve(_ack, dict(_WINDOW_CARD), w.client())


def slack_window_reject(w):
    return handlers.handle_ro_window_reject(_ack, dict(_WINDOW_CARD), w.client())


PENDING_ACCESS = idp_outbox.ACCESS_REQUEST_PENDING
DECIDED_ACCESS = idp_outbox.ACCESS_REQUEST_DECIDED
PENDING_WINDOW = idp_outbox.WINDOW_REQUEST_PENDING
DECIDED_WINDOW = idp_outbox.WINDOW_REQUEST_DECIDED

# (path, its event, its recipients)
PATHS = [
    pytest.param(web_access_create, PENDING_ACCESS, "admins", id="web-access-create"),
    pytest.param(slack_access_create, PENDING_ACCESS, "admins", id="slack-access-create"),
    pytest.param(web_access_approve, DECIDED_ACCESS, "requester", id="web-access-approve"),
    pytest.param(web_access_reject, DECIDED_ACCESS, "requester", id="web-access-reject"),
    pytest.param(slack_access_approve, DECIDED_ACCESS, "requester", id="slack-access-approve"),
    pytest.param(slack_access_reject, DECIDED_ACCESS, "requester", id="slack-access-reject"),
    pytest.param(web_window_create, PENDING_WINDOW, "admins", id="web-window-create"),
    pytest.param(slack_window_create, PENDING_WINDOW, "admins", id="slack-window-create"),
    pytest.param(web_window_approve, DECIDED_WINDOW, "requester", id="web-window-approve"),
    pytest.param(web_window_reject, DECIDED_WINDOW, "requester", id="web-window-reject"),
    pytest.param(slack_window_approve, DECIDED_WINDOW, "requester", id="slack-window-approve"),
    pytest.param(slack_window_reject, DECIDED_WINDOW, "requester", id="slack-window-reject"),
]
WEB_PATHS = [p for p in PATHS if p.id.startswith("web-")]


# ---- every path ---------------------------------------------------------------

@pytest.mark.parametrize("run,event,who", PATHS)
def test_each_path_writes_one_row_of_its_event(w, run, event, who):
    run(w)

    assert len(w.rows) == 1, w.rows
    sql, (event_type, recipients, payload_json) = w.rows[0]
    assert event_type == event
    # request_id holds a SQL request's id (requests.id). These requests have
    # their own ids, so the column stays NULL and the payload carries the id.
    assert "VALUES (%s, NULL, %s, %s)" in sql
    if who == "admins":
        # The admins that QueryHub told Slack to DM. The requester's DM is not one.
        dmed = [u for u in w.slack.opened if u != REQUESTER]
        assert dmed and recipients == dmed
    else:
        assert recipients == [REQUESTER]
    payload = json.loads(payload_json)
    assert payload and all(isinstance(v, str) for v in payload.values()), payload


@pytest.mark.parametrize("run,event,who", PATHS)
def test_with_the_switch_off_no_path_writes_a_row(w, run, event, who):
    w.flag = "off"
    run(w)
    assert w.rows == []


@pytest.mark.parametrize("run,event,who", PATHS)
def test_the_outbox_changes_nothing_that_the_caller_slack_or_the_tables_see(
        w, run, event, who, caplog):
    """The answer, every Slack message and card, and every other statement
    (decision, grant, audit row) are the same with the switch off, on, and on
    with a failing insert."""
    seen = {}
    for mode in ("off", "on", "failing"):
        w.reset()
        w.flag = "off" if mode == "off" else "on"
        w.fail_insert = mode == "failing"
        caplog.clear()
        seen[mode] = (run(w), w.slack.sent, w.sql)
        if mode == "failing":
            assert w.rows == []
            assert f"failed to write the {event} notification_outbox row" in caplog.text
    assert seen["on"] == seen["off"]
    assert seen["failing"] == seen["off"]


@pytest.mark.parametrize("run,event,who", WEB_PATHS)
def test_without_slack_the_web_path_still_writes_the_row(w, run, event, who):
    w.env.slack_enabled = False

    run(w)

    assert w.slack.opened == [] and w.slack.sent == []
    assert len(w.rows) == 1
    _, (event_type, recipients, _payload) = w.rows[0]
    assert event_type == event
    # The first admin list is the fan-out's own (two admins, see the fixture).
    assert recipients == (["U0ADMIN1", "U0ADMIN2"] if who == "admins" else [REQUESTER])


def test_with_slack_and_the_switch_off_the_fan_outs_read_no_admin_list(w):
    w.env.slack_enabled = False
    w.flag = "off"

    assert access.fan_out_admin_dms(None, dict(w.access_row), TARGET) is None
    assert aar.notify_admins(None, dict(w.window_row), "demo-orders") == 0

    assert w.list_active_calls == 0 and w.rows == []


@pytest.mark.parametrize("run", [
    pytest.param(web_access_approve, id="web-access"),
    pytest.param(slack_access_approve, id="slack-access"),
    pytest.param(web_window_approve, id="web-window-approve"),
    pytest.param(slack_window_approve, id="slack-window-approve"),
    pytest.param(slack_window_reject, id="slack-window-reject"),
])
def test_a_decision_that_loses_the_race_writes_no_row(w, run):
    w.race = True
    with contextlib.suppress(HTTPException):
        run(w)
    assert w.rows == []


# ---- the payloads -------------------------------------------------------------

def test_access_request_pending_payload(w):
    web_access_create(w)
    assert w.payloads() == [{
        "requestId": "12", "requesterName": "Dev One", "target": "demo-orders",
        "database": "demo_db", "tier": "ro",
        "reason": "[requested tier: RO] need read access"}]


def test_access_request_decided_payloads(w):
    approved = {"requestId": "12", "decision": "approved", "target": "demo-orders",
                "database": "demo_db", "tier": "ro", "deciderName": "Admin One",
                "note": "", "grantApplied": "true"}
    web_access_approve(w)
    assert w.payloads() == [approved]
    w.reset()
    web_access_reject(w)
    assert w.payloads() == [dict(approved, decision="rejected", note="too broad",
                                 grantApplied="false")]


def test_an_approval_that_skips_the_grant_says_so(w):
    """No target yet: the approval writes no grant, so the requester has no
    access. The Slack DM says "a DBA will finalize", and the row says false."""
    w.access_row.update(target_server_id=None)
    web_access_approve(w)
    assert w.payloads()[0]["grantApplied"] == "false"


def test_window_request_pending_payload(w):
    web_window_create(w)
    assert w.payloads() == [{
        "requestId": "7", "requesterName": "Dev One", "target": "demo-orders",
        "database": "", "tier": "ro", "reason": "reading the ledger",
        "windowLabel": "1 day"}]


def test_window_request_decided_payloads(w):
    web_window_approve(w)
    approved = {"requestId": "7", "decision": "approved", "target": "demo-orders",
                "database": "", "tier": "ro", "deciderName": "Admin One",
                "note": "", "windowLabel": "1 day"}
    assert w.payloads() == [approved]
    w.reset()
    slack_window_reject(w)
    assert w.payloads() == [dict(approved, decision="rejected", deciderName="admin.one")]


def test_a_sparse_row_still_gives_strings_only(w):
    """Ruling P-10: a NULL column is "", never null."""
    sparse = {"id": 5, "requester_slack_id": REQUESTER, "requester_name": None,
              "target_server_id": None, "database_name": None,
              "attempted_query": None, "reason": None, "requested_tier": None,
              "status": "rejected", "decided_by_slack_id": None,
              "decided_by_name": None, "decision_reason": None,
              "max_tier": None, "window_minutes": None}
    one_admin = [{"slack_user_id": ADMIN}]
    idp_outbox.access_request_pending(sparse, None, one_admin)
    idp_outbox.access_request_decided(sparse)
    idp_outbox.window_request_pending(sparse, "demo-orders", one_admin)
    idp_outbox.window_request_decided(sparse, approved=False, alias="demo-orders",
                                      decider_name=None)

    payloads = w.payloads()
    assert len(payloads) == 4
    for payload in payloads:
        assert all(isinstance(v, str) for v in payload.values()), payload
    assert payloads[0] == {"requestId": "5", "requesterName": REQUESTER, "target": "",
                           "database": "", "tier": "ro", "reason": ""}
    assert payloads[1]["deciderName"] == "" and payloads[1]["note"] == ""
    assert payloads[3]["windowLabel"] == "" and payloads[3]["tier"] == "ro"


def test_a_server_that_is_not_onboarded_keeps_its_typed_name_in_both_events(w):
    body = rr.EndpointRequestIn(server="new-cluster", database="demo_db",
                                tier="RW", reason="need read access")
    rr.endpoint_request(body, claims={"sub": REQUESTER, "name": "Dev One"})
    # Decide the row exactly as the web route stored it.
    w.access_row = dict(w.created[-1])
    web_access_reject(w)

    assert [p["target"] for p in w.payloads()] == ["new-cluster", "new-cluster"]


def test_a_row_with_no_admins_is_not_written(w):
    idp_outbox.access_request_pending(dict(w.access_row), TARGET, [])
    idp_outbox.window_request_pending(dict(w.window_row), "demo-orders", [])
    assert w.rows == []


# ---- the switch ---------------------------------------------------------------

def test_the_switch_is_off_by_default(monkeypatch):
    """conftest answers every bot_config read with the default the caller gives."""
    assert idp_outbox.enabled() is False


def test_a_failing_switch_read_counts_as_off(w, caplog):
    w.flag_error = True

    out = web_access_create(w)

    assert out["status"] == "submitted"
    assert w.rows == []
    assert "could not read idp_outbox_enabled" in caplog.text


# ---- the read side ------------------------------------------------------------

def test_the_read_route_serves_a_decided_event_to_a_requester_who_is_no_admin(
        w, monkeypatch):
    """The read route resolves recipients in `admins`, then in `requesters`.
    A decided event goes to a requester, so the second lookup is the one that
    must find them."""
    web_access_reject(w)
    _, (event_type, recipients, payload_json) = w.rows[0]
    stored = {"id": 1, "event_type": event_type, "request_id": None,
              "recipients": recipients, "payload": json.loads(payload_json),
              "created_at": datetime(2026, 10, 14, tzinfo=timezone.utc)}

    def fetch_all(sql, params=None):
        if "FROM notification_outbox" in sql:
            return [stored]
        if "FROM admins" in sql:
            return []
        if "FROM requesters" in sql:
            return [{"slack_user_id": REQUESTER, "email": "dev.one@example.com"}]
        raise AssertionError(f"unexpected fetch_all: {sql!r}")
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(ra.admin, "require_sync_principal", lambda claims: None)

    out = ra.notifications_outbox(limit=50, claims={"sub": "U0SYNC"})

    assert out["unresolvedRecipients"] == 0
    event = out["events"][0]
    assert event["eventType"] == DECIDED_ACCESS
    assert event["requestId"] is None
    assert event["recipients"] == ["dev.one@example.com"]
    assert event["payload"] == stored["payload"]
