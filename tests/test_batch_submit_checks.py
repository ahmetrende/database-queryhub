"""Batch submit: quota, Slack item validation, and the stored engine and tier.

No live DB. Each test stubs the lookups the code under test makes.
"""
from __future__ import annotations

from types import SimpleNamespace

from queryhub import admins, bundles, core_submit as cs, requesters, targets, teams
from queryhub import auto_approve, lifecycle
from queryhub.slack_app import handlers, modal

USER = "U0EXAMPLE001"


# --- B10: the quota counts the whole batch ----------------------------------

class _QuotaCur:
    def __init__(self, open_count):
        self.open_count = open_count
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
        return {"n": self.open_count}


def test_quota_counts_every_item_of_the_batch(monkeypatch):
    monkeypatch.setattr(admins, "is_admin", lambda uid: False)
    # The default limit is 5. Three open requests leave room for two items.
    fits = _QuotaCur(3)
    assert bundles.check_open_quota(fits, USER, 2) is None
    over = _QuotaCur(3)
    message = bundles.check_open_quota(over, USER, 3)
    assert message is not None
    assert "6 open requests" in message and "limit is 5" in message


def test_quota_takes_the_advisory_lock_first(monkeypatch):
    monkeypatch.setattr(admins, "is_admin", lambda uid: False)
    cur = _QuotaCur(0)
    bundles.check_open_quota(cur, USER, 1)
    assert "pg_advisory_xact_lock" in cur.sql[0]


def test_quota_exempts_an_admin_but_still_locks(monkeypatch):
    monkeypatch.setattr(admins, "is_admin", lambda uid: True)
    cur = _QuotaCur(99)
    assert bundles.check_open_quota(cur, USER, 5) is None
    assert len(cur.sql) == 1 and "pg_advisory_xact_lock" in cur.sql[0]


# --- B23: a Slack batch item runs the full submit checks --------------------

def _stub_world(monkeypatch, configured):
    target = SimpleNamespace(id=1, alias="example-1", engine="postgres",
                             default_database="ledger", replica_of=None, enabled=True)
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(requesters, "open_request_count", lambda uid: 0)
    monkeypatch.setattr(targets, "get", lambda tid: target)
    monkeypatch.setattr(targets, "configured_tiers", lambda ids: {1: set(configured)})
    monkeypatch.setattr(teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "rw"})
    monkeypatch.setattr(teams, "effective_mode_for_database",
                        lambda uid, tid, db_name: "rw")
    monkeypatch.setattr(auto_approve, "decision", lambda *a, **k: None)
    monkeypatch.setattr(cs.db, "fetch_one", lambda *a, **k: None)


def test_a_slack_item_for_a_tier_without_credentials_is_refused(monkeypatch):
    _stub_world(monkeypatch, configured={"ro"})
    item, errors = handlers._validate_batch_item(
        user_id=USER,
        raw_item={"_index": 2, "target_server_id": 1,
                  "query": "UPDATE ledgers SET x = 1 WHERE id = 7"},
        bundle_justification="fix a wrong value")
    assert item is None
    assert list(errors) == [f"{modal.BATCH_B_QUERY}_2"]
    assert "not ready for RW" in errors[f"{modal.BATCH_B_QUERY}_2"]


def test_a_valid_slack_item_carries_engine_and_tier(monkeypatch):
    _stub_world(monkeypatch, configured={"ro", "rw"})
    item, errors = handlers._validate_batch_item(
        user_id=USER,
        raw_item={"_index": 1, "target_server_id": 1,
                  "query": "UPDATE ledgers SET x = 1 WHERE id = 7"},
        bundle_justification="fix a wrong value")
    assert errors == {}
    assert item["engine"] == "postgres"
    assert item["required_tier"] == "rw"
    assert item["explain_plan"] is None


# --- B24: batch rows store engine and required_tier -------------------------

class _InsertCur:
    def __init__(self):
        self.item_params = []
        self.item_sql = ""
        self._n = 0

    def execute(self, sql, params=None):
        if "INSERT INTO requests" in sql:
            self.item_sql = sql
            self.item_params.append(params)
            self._n += 1
            self._row = {"id": self._n}
        else:
            self._row = {"id": 9, "created_at": None}

    def fetchone(self):
        return self._row


def test_batch_rows_carry_engine_and_required_tier():
    items = [{"target_server_id": 1, "database_name": "d", "query": "SELECT 1",
              "wants_result": True, "explain_plan": None,
              "engine": "postgres", "required_tier": "ro"},
             {"target_server_id": 2, "database_name": "d", "query": "SELECT 2",
              "wants_result": True, "explain_plan": None,
              "engine": "mssql", "required_tier": "rw"}]
    cur = _InsertCur()
    bundles.insert_bundle_with_items(
        cur, requester_slack_id=USER, requester_name="Ex", justification=None,
        scheduled_for=None, items=items)
    assert "engine, required_tier" in cur.item_sql
    # Order at the end of the row: engine, required_tier, default_schema,
    # result_to_slack.
    assert [p[-4:-2] for p in cur.item_params] == [
        ("postgres", "ro"), ("mssql", "rw")]
