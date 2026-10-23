"""How a request closed, and when a pending one expires (PLA-1592 U11).

The web shows four different ends as `rejected`: an admin's refusal, a request
for changes, a request that its requester withdrew, and a request that the
expiry job closed. The withdrawn one then read as a refusal by a DBA.

- `outcome` tells the four apart: withdrawn, expired, denied,
  changes_requested, or null. `GET /queries/{id}`, every queue item and every
  `GET /history` row carry it. `status` keeps its values.
- The expiry job sets `cancelled`, the same status as a withdrawal. Its own
  audit action, `request_expired`, is written in the transaction of the
  status change, so it is the marker. A withdrawal writes `withdrawn` or
  `cancelled`.
- `expiresAt` is when the expiry job may close a pending request:
  `created_at + pending_expiry_hours`, and null while `pending_expiry_enabled`
  is off. The job and the web read the two keys through `pending_expiry`.
"""
from __future__ import annotations

import importlib.util
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from queryhub import config as cfg
from queryhub import pending_expiry
from queryhub.web import app as web_app
from queryhub.web import deps, mapping, routes_admin, routes_data, routes_queries

ME = "U0EXAMPLE001"
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _settings(monkeypatch, **values):
    """bot_config as the test wants it. Unlisted keys give their default."""
    def get_setting(key, default=None):
        if key in values:
            return values[key]
        if default is None:
            raise KeyError(key)
        return default
    monkeypatch.setattr(cfg, "get_setting", get_setting)


# ---- the rule ------------------------------------------------------------------

@pytest.mark.parametrize("status,actions,outcome", [
    ("rejected", (), "denied"),
    ("changes_requested", (), "changes_requested"),
    ("cancelled", {"request_expired"}, "expired"),
    ("cancelled", {"withdrawn"}, "withdrawn"),            # the web withdraw
    ("cancelled", {"cancelled"}, "withdrawn"),            # Slack, scheduled, edit
    ("cancelled", {"submitted", "cancelled"}, "withdrawn"),
    ("cancelled", set(), None),                           # no marker: unknown
    ("cancelled", {"submitted"}, None),
])
def test_the_outcome_of_each_closed_request(status, actions, outcome):
    assert mapping.request_outcome(status, actions) == outcome


@pytest.mark.parametrize("status", [
    "pending", "approved", "scheduled", "executing", "completed", "failed",
    "awaiting_dba_manual", "draft", None])
def test_a_request_that_did_not_close_without_running_has_none(status):
    """The audit actions do not matter here: only a cancelled request reads
    them."""
    assert mapping.request_outcome(status, {"request_expired", "withdrawn"}) is None


def test_an_expiry_marker_wins_over_a_withdrawal_marker():
    """Both cannot happen to one request: each needs it pending. If both were
    there, the job closed it, because the job writes nothing else."""
    assert mapping.request_outcome("cancelled", {"cancelled", "request_expired"}) \
        == "expired"


def test_status_keeps_its_values():
    """Additive: the four ends still show as `rejected`."""
    for status in ("rejected", "changes_requested", "cancelled"):
        assert mapping.status_to_web(status) == "rejected"


def test_the_markers_are_the_actions_that_the_writers_write():
    """If a writer changes its action, the outcome goes null. This pins the
    names that the writers use today."""
    import inspect

    from queryhub import cancellation, core_submit
    from queryhub.slack_app import handlers
    assert '"withdrawn"' in inspect.getsource(cancellation.withdraw)
    assert '"cancelled"' in inspect.getsource(core_submit.create_request)
    assert '"cancelled"' in inspect.getsource(routes_queries.scheduled_cancel)
    assert '"cancelled"' in inspect.getsource(handlers.handle_cancel_request)
    assert '"cancelled"' in inspect.getsource(handlers.handle_cancel_scheduled)
    script = (ROOT / "scripts" / "expire_stale_pending.py").read_text(encoding="utf-8")
    assert '"request_expired"' in script
    assert mapping.EXPIRY_ACTION == "request_expired"
    assert set(mapping.OUTCOME_ACTIONS) == {"request_expired", "withdrawn", "cancelled"}


# ---- the expiry time -----------------------------------------------------------

def test_no_window_while_expiry_is_off(monkeypatch):
    _settings(monkeypatch, pending_expiry_enabled="off", pending_expiry_hours="24")
    assert pending_expiry.window() is None


@pytest.mark.parametrize("value", ["on", "ON", " true ", "yes", "1"])
def test_the_window_is_the_configured_hours(monkeypatch, value):
    _settings(monkeypatch, pending_expiry_enabled=value, pending_expiry_hours="6")
    assert pending_expiry.window() == timedelta(hours=6)


def test_hours_that_are_not_a_number_give_no_window(monkeypatch):
    """The job stops on such a value, so nothing expires. The web says so."""
    _settings(monkeypatch, pending_expiry_enabled="on", pending_expiry_hours="soon")
    assert pending_expiry.window() is None


def test_only_a_pending_request_expires():
    span = timedelta(hours=24)
    assert pending_expiry.expires_at({"status": "pending", "created_at": NOW}, span) \
        == NOW + span
    for status in ("approved", "scheduled", "changes_requested", "cancelled"):
        assert pending_expiry.expires_at({"status": status, "created_at": NOW}, span) \
            is None, status
    assert pending_expiry.expires_at({"status": "pending", "created_at": NOW}, None) \
        is None
    assert pending_expiry.expires_at({"status": "pending", "created_at": None}, span) \
        is None


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "expire_stale_pending_under_test", ROOT / "scripts" / "expire_stale_pending.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_job_reads_the_switch_through_the_shared_rule(monkeypatch):
    script = _load_script()
    monkeypatch.setattr(pending_expiry, "enabled", lambda: True)
    assert script._expiry_enabled() is True
    monkeypatch.setattr(pending_expiry, "enabled", lambda: False)
    assert script._expiry_enabled() is False


def test_the_job_reads_the_hours_through_the_shared_rule(monkeypatch):
    script = _load_script()
    asked = []
    monkeypatch.setattr(pending_expiry, "hours", lambda: 7)
    monkeypatch.setattr(script.db, "init_pool", lambda: None)
    monkeypatch.setattr(script, "find_stale", lambda h: asked.append(h) or [])
    monkeypatch.setattr("sys.argv", ["expire_stale_pending.py"])
    assert script.main() == 0
    assert asked == [7]


# ---- GET /queries/{id} -----------------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    logging.disable(logging.CRITICAL)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": ME, "provider": "slack", "sid": "s1", "name": "Jordan Ray"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: True)
    yield TestClient(app)
    logging.disable(logging.NOTSET)


def _request(**kw):
    row = {"id": 41, "requester_slack_id": ME, "target_server_id": 7,
           "database_name": "orders", "query": "SELECT 1", "status": "pending",
           "created_at": NOW, "decided_at": None, "decided_by_slack_id": None,
           "decided_by_name": None, "decision_reason": None, "executed_at": None,
           "completed_at": None, "scheduled_for": None, "row_count": None,
           "required_tier": "ro", "engine": "postgres"}
    row.update(kw)
    return row


def _status(web, monkeypatch, row, actions):
    reads = []
    monkeypatch.setattr(routes_queries.db, "fetch_one", lambda sql, p=None: dict(row))
    monkeypatch.setattr(
        routes_queries.db, "fetch_all",
        lambda sql, p=None: reads.append(sql) or [
            {"actor_slack_id": None, "actor_name": None, "action": a,
             "created_at": NOW, "details": {}} for a in actions])
    monkeypatch.setattr(routes_queries, "_alias_of", lambda tid: "prod-main")
    monkeypatch.setattr(routes_queries.people, "display_name", lambda *a, **k: "Sam Ellis")
    r = web.get(f"/api/queries/{row['id']}")
    assert r.status_code == 200, r.text
    return r.json(), reads


@pytest.mark.parametrize("actions,outcome", [
    (["submitted", "request_expired"], "expired"),
    (["submitted", "withdrawn"], "withdrawn"),
    (["submitted", "cancelled"], "withdrawn"),
])
def test_the_status_poll_tells_expired_from_withdrawn(web, monkeypatch, actions, outcome):
    body, reads = _status(web, monkeypatch,
                          _request(status="cancelled", decided_at=NOW), actions)
    assert body["status"] == "rejected"          # unchanged
    assert body["outcome"] == outcome
    assert body["expiresAt"] is None             # closed: nothing left to expire
    assert len(reads) == 1, "the outcome cost a second audit read"


def test_the_status_poll_says_denied_for_a_refusal(web, monkeypatch):
    body, _ = _status(web, monkeypatch,
                      _request(status="rejected", decided_at=NOW,
                               decided_by_slack_id="U0EXAMPLE009",
                               decided_by_name="Sam Ellis", decision_reason="too broad"),
                      ["submitted", "rejected"])
    assert (body["status"], body["outcome"]) == ("rejected", "denied")


def test_a_pending_request_carries_its_expiry_while_expiry_is_on(web, monkeypatch):
    _settings(monkeypatch, pending_expiry_enabled="on", pending_expiry_hours="24")
    body, _ = _status(web, monkeypatch, _request(), ["submitted"])
    assert body["expiresAt"] == (NOW + timedelta(hours=24)).isoformat()
    assert body["outcome"] is None


def test_no_expiry_time_while_expiry_is_off(web, monkeypatch):
    _settings(monkeypatch, pending_expiry_enabled="off", pending_expiry_hours="24")
    body, _ = _status(web, monkeypatch, _request(), ["submitted"])
    assert "expiresAt" in body and body["expiresAt"] is None


# ---- GET /history ------------------------------------------------------------------

def _history(web, monkeypatch, rows, audits):
    reads = []

    def fetch_all(sql, p=None):
        flat = " ".join(sql.split())
        reads.append((flat, p))
        if flat.startswith("SELECT id, query, target_server_id"):
            return [dict(r) for r in rows]
        if "FROM audit_log" in flat:
            return [dict(a) for a in audits if a["request_id"] in p[0]]
        raise AssertionError(f"unexpected read: {flat}")
    monkeypatch.setattr(routes_data.db, "fetch_all", fetch_all)
    monkeypatch.setattr(routes_data, "_alias_of", lambda tid: "prod-main")
    monkeypatch.setattr(routes_data, "_conn_state_resolver", lambda uid: lambda tid: "ok")
    monkeypatch.setattr(routes_data.people, "namer", lambda names: lambda n: n)
    r = web.get("/api/history")
    assert r.status_code == 200, r.text
    return {h["id"]: h for h in r.json()["history"]}, reads


def _hrow(rid, status, **kw):
    row = {"id": rid, "query": "SELECT 1", "target_server_id": 7,
           "database_name": "orders", "status": status, "row_count": None,
           "created_at": NOW, "decided_by_slack_id": None, "decided_by_name": None,
           "requester_slack_id": ME, "required_tier": "ro", "executed_at": None,
           "executed_target_id": None, "executed_tier": None, "engine": "postgres"}
    row.update(kw)
    return row


def test_every_history_row_carries_its_outcome(web, monkeypatch):
    rows = [_hrow(6, "cancelled"), _hrow(5, "cancelled"), _hrow(4, "rejected"),
            _hrow(3, "changes_requested"), _hrow(2, "pending"), _hrow(1, "cancelled")]
    audits = [{"request_id": 6, "action": "request_expired"},
              {"request_id": 5, "action": "withdrawn"}]
    got, reads = _history(web, monkeypatch, rows, audits)
    assert {k: (v["status"], v["outcome"]) for k, v in got.items()} == {
        "6": ("rejected", "expired"),
        "5": ("rejected", "withdrawn"),
        "4": ("rejected", "denied"),
        "3": ("rejected", "changes_requested"),
        "2": ("pending", None),
        "1": ("rejected", None),                  # cancelled with no marker
    }
    outcome_reads = [(sql, p) for sql, p in reads if "FROM audit_log" in sql]
    assert len(outcome_reads) == 1
    sql, params = outcome_reads[0]
    assert "request_id = ANY(%s) AND action = ANY(%s)" in sql
    assert params[0] == [6, 5, 1]                 # the cancelled rows only
    assert set(params[1]) == {"request_expired", "withdrawn", "cancelled"}


def test_a_page_without_a_cancelled_row_asks_nothing_for_the_outcome(web, monkeypatch):
    got, reads = _history(web, monkeypatch,
                          [_hrow(2, "rejected"), _hrow(1, "pending")], [])
    assert got["2"]["outcome"] == "denied"
    assert not any("FROM audit_log" in sql for sql, _p in reads)


def test_without_the_audit_resolver_a_history_entry_keeps_its_old_shape():
    entry = mapping.history_entry(_hrow(1, "cancelled"), lambda tid: "prod-main")
    assert "outcome" not in entry


# ---- the queue ---------------------------------------------------------------------

@pytest.fixture
def queue(monkeypatch):
    rows = [{"id": 8, "requester_slack_id": "U0EXAMPLE002", "requester_name": "Jordan Ray",
             "target_server_id": 7, "database_name": "orders", "query": "SELECT 1",
             "justification": None, "status": "pending", "scheduled_for": None,
             "bundle_id": None, "position": None, "risk_summary": None,
             "origin": "web", "created_at": NOW, "engine": "postgres",
             "required_tier": "ro", "bundle_size": 1}]
    monkeypatch.setattr(routes_admin.admin, "require_admin",
                        lambda claims, need, **kw: claims["sub"])
    monkeypatch.setattr(routes_admin.db, "fetch_all", lambda sql, p=None: [dict(r) for r in rows])
    monkeypatch.setattr(routes_admin.admins, "can_approve", lambda uid, row: True)
    monkeypatch.setattr(routes_admin.people, "namer", lambda names: lambda n: n)
    monkeypatch.setattr(routes_admin, "_alias_of", lambda tid: "prod-main")
    monkeypatch.setattr(routes_admin, "_tags_of", lambda tid: {})
    monkeypatch.setattr(routes_admin.mapping, "pii_preview",
                        lambda q: {"columns": [], "star": False})
    return lambda: routes_admin.admin_queue(claims={"sub": ME})["queue"]


def test_a_queue_item_carries_its_expiry_while_expiry_is_on(queue, monkeypatch):
    _settings(monkeypatch, pending_expiry_enabled="on", pending_expiry_hours="1")
    item = queue()[0]
    assert item["expiresAt"] == (NOW + timedelta(hours=1)).isoformat()
    assert item["outcome"] is None


def test_a_queue_item_has_no_expiry_while_expiry_is_off(queue, monkeypatch):
    _settings(monkeypatch, pending_expiry_enabled="off", pending_expiry_hours="1")
    item = queue()[0]
    assert item["expiresAt"] is None and item["outcome"] is None
