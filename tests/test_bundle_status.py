"""GET /bundles/{id}: the state of each item of a bundle (no live DB).

The fleet run page polls this one URL. Each item gets one of five states, the
page counts them and names every failure. Only the submitter reads a bundle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from queryhub import executor
from queryhub.web import mapping, routes_queries as rq

OWNER = "U0EXAMPLE001"
OTHER = "U0EXAMPLE002"
T0 = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)


# ---- mapping helpers ---------------------------------------------------------

@pytest.mark.parametrize("status,state", [
    ("pending", "waiting"), ("approved", "waiting"), ("scheduled", "waiting"),
    ("executing", "running"),
    ("completed", "ok"),
    ("failed", "failed"), ("awaiting_dba_manual", "failed"),
    ("rejected", "not_run"), ("cancelled", "not_run"),
    ("changes_requested", "not_run"), ("expired", "not_run"),
    (None, "not_run"),
])
def test_bundle_item_state(status, state):
    assert mapping.bundle_item_state(status) == state


def test_skipped_outcome_is_not_a_withdrawal():
    # A stop run cancels the rest with its own action. Before this, the action
    # was `cancelled`, and the item read as "withdrawn by you".
    assert mapping.request_outcome("cancelled", {"fleet_skipped"}) == "skipped"
    assert mapping.request_outcome("cancelled", {"cancelled"}) == "withdrawn"
    assert "fleet_skipped" in mapping.OUTCOME_ACTIONS
    assert mapping.FLEET_SKIPPED_ACTION == executor.FLEET_SKIPPED_ACTION
    assert mapping.audit_kind("fleet_skipped") == "reject"


def test_detail_of_a_failed_item_is_its_error():
    assert mapping.bundle_item_detail(
        {"status": "failed", "error_message": "relation \"x\" does not exist"},
        None) == "relation \"x\" does not exist"
    assert mapping.bundle_item_detail({"status": "failed"}, None) == "Execution failed."


def test_detail_of_an_item_that_needs_a_dba():
    d = mapping.bundle_item_detail(
        {"status": "awaiting_dba_manual",
         "error_message": "requires DBA manual execution — needs a superuser."},
        None)
    assert d == "QueryHub could not run this: needs a superuser. A DBA must run it manually."


def test_detail_of_items_that_did_not_run():
    skip = "Not run: srv2 failed first. The fleet run stops at the first failure."
    assert mapping.bundle_item_detail(
        {"status": "cancelled", "error_message": skip}, "skipped") == skip
    assert mapping.bundle_item_detail({"status": "cancelled"}, "skipped") == (
        "Skipped: an earlier item failed.")
    assert mapping.bundle_item_detail(
        {"status": "rejected", "decision_reason": "Use the replica."}, "denied") == (
        "Denied: Use the replica.")
    assert mapping.bundle_item_detail({"status": "rejected"}, "denied") == "Denied"
    # A withdrawal and an expiry carry no note of a DBA.
    assert mapping.bundle_item_detail(
        {"status": "cancelled", "decision_reason": "Cancelled."}, "withdrawn") == "Withdrawn"
    assert mapping.bundle_item_detail({"status": "cancelled"}, "expired") == "Expired"
    assert mapping.bundle_item_detail({"status": "cancelled"}, None) == "Not run."


def test_no_detail_for_an_item_that_ran_or_waits():
    for st in ("completed", "executing", "pending", "approved"):
        assert mapping.bundle_item_detail({"status": st, "error_message": "x"}, None) is None


def test_counts_have_every_state():
    assert mapping.bundle_counts(["ok", "ok", "failed"]) == {
        "ok": 2, "failed": 1, "not_run": 0, "running": 0, "waiting": 0}


# ---- the route ---------------------------------------------------------------

def _req(i, status, *, query="SELECT 1", tid=101, **kw):
    row = {"id": i, "position": i - 10, "target_server_id": tid,
           "database_name": f"db{i}", "query": query, "status": status,
           "row_count": None, "executed_at": None, "completed_at": None,
           "error_message": None, "decision_reason": None}
    row.update(kw)
    return row


@pytest.fixture
def world(monkeypatch):
    st = {"bundle": {"id": 7, "kind": "fleet", "on_failure": "stop",
                     "justification": "check the setting", "created_at": T0,
                     "scheduled_for": None, "requester_slack_id": OWNER},
          "rows": [], "acts": [], "calls": [], "alias_calls": []}

    def fetch_one(sql, params=None):
        st["calls"].append(sql)
        assert "FROM request_bundles" in sql
        b = st["bundle"]
        if params != (b["id"], b["requester_slack_id"]):
            return None
        return b

    def fetch_all(sql, params=None):
        st["calls"].append(sql)
        if "FROM requests" in sql:
            assert params == (st["bundle"]["id"], OWNER)
            return st["rows"]
        if "FROM audit_log" in sql:
            ids, actions = params
            assert set(actions) == set(mapping.OUTCOME_ACTIONS)
            return [a for a in st["acts"] if a["request_id"] in ids]
        raise AssertionError(sql)

    def alias_of(tid):
        st["alias_calls"].append(tid)
        return f"example-{tid - 100}"

    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq.db, "fetch_one", fetch_one)
    monkeypatch.setattr(rq.db, "fetch_all", fetch_all)
    monkeypatch.setattr(rq, "_alias_of", alias_of)
    return st


def test_someone_else_gets_404(world):
    with pytest.raises(HTTPException) as e:
        rq.bundle_status(7, claims={"sub": OTHER})
    assert e.value.status_code == 404
    # Nothing else is read for a bundle the caller does not own.
    assert len(world["calls"]) == 1


def test_items_carry_state_counts_and_details(world):
    world["rows"] = [
        _req(11, "completed", row_count=3, executed_at=T0,
             completed_at=T0 + timedelta(milliseconds=250)),
        _req(12, "failed", tid=102, error_message="permission denied for table x"),
        _req(13, "cancelled", tid=101,
             error_message="Not run: example-2 failed first. The fleet run stops at the first failure."),
        _req(14, "executing", tid=103),
    ]
    world["acts"] = [{"request_id": 13, "action": "fleet_skipped"}]
    r = rq.bundle_status(7, claims={"sub": OWNER})
    assert r["bundleId"] == "7"
    assert r["kind"] == "fleet" and r["onFailure"] == "stop"
    assert r["sql"] == "SELECT 1"
    assert r["counts"] == {"ok": 1, "failed": 1, "not_run": 1, "running": 1, "waiting": 0}
    assert r["done"] is False
    by = {i["queryId"]: i for i in r["items"]}
    assert [i["queryId"] for i in r["items"]] == ["11", "12", "13", "14"]
    assert by["11"]["state"] == "ok" and by["11"]["rowCount"] == 3 and by["11"]["runMs"] == 250
    assert by["11"]["connectionId"] == "example-1" and by["11"]["databaseId"] == "db11"
    assert by["12"]["state"] == "failed"
    assert by["12"]["detail"] == "permission denied for table x"
    assert by["12"]["rowCount"] is None
    assert by["13"]["state"] == "not_run" and by["13"]["outcome"] == "skipped"
    assert by["13"]["detail"].startswith("Not run: example-2 failed first.")
    assert by["14"]["state"] == "running" and by["14"]["status"] == "running"
    # One alias read per target, not one per item.
    assert sorted(world["alias_calls"]) == [101, 102, 103]


def test_done_when_no_item_waits_or_runs(world):
    world["rows"] = [_req(11, "completed", row_count=0), _req(12, "rejected")]
    r = rq.bundle_status(7, claims={"sub": OWNER})
    assert r["done"] is True
    assert r["items"][1]["outcome"] == "denied"


def test_a_batch_with_two_texts_has_no_single_sql(world):
    world["bundle"]["kind"] = "batch"
    world["bundle"]["on_failure"] = None
    world["rows"] = [_req(11, "pending", query="SELECT 1"),
                     _req(12, "pending", query="SELECT 2")]
    r = rq.bundle_status(7, claims={"sub": OWNER})
    assert r["kind"] == "batch" and r["sql"] is None
    assert r["counts"]["waiting"] == 2 and r["done"] is False


def test_an_empty_bundle_reads_no_audit(world):
    r = rq.bundle_status(7, claims={"sub": OWNER})
    assert r["items"] == [] and r["sql"] is None
    assert not any("audit_log" in c for c in world["calls"])


def test_a_history_row_names_its_bundle():
    row = {"id": 11, "query": "SELECT 1", "target_server_id": 101,
           "database_name": "db11", "status": "completed", "bundle_id": 7}
    entry = mapping.history_entry(row, lambda tid: "example-1")
    assert entry["bundleId"] == "7"
    assert mapping.history_entry({**row, "bundle_id": None}, lambda tid: "x")["bundleId"] is None
