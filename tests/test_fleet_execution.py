"""Fleet run execution chain, the admin re-check and the compact Slack card."""
import threading
import time

import pytest

from queryhub import admins, bundles, executor as ex, lifecycle
from queryhub.slack_app import handlers, notifications


class _Future:
    def add_done_callback(self, fn):
        fn(self)

    def exception(self):
        return None


class _SyncPool:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))
        fn(*args)
        return _Future()


class _HoldPool(_SyncPool):
    """Records submissions and never runs them (the chain stays 'running')."""
    def submit(self, fn, *args):
        self.calls.append((fn, args))
        return _Future()


class _Cur:
    def __init__(self, state):
        self.state = state
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        # Any other statement (a _fail UPDATE, an audit INSERT) touches one row.
        self.rowcount = 1
        if "SET status = 'cancelled'" in sql:
            reason = params[0]
            self._rows = []
            for it in self.state["items"]:
                if it["status"] in ("approved", "pending"):
                    it["status"] = "cancelled"
                    it["reason"] = reason
                    self._rows.append({"id": it["id"]})

    def fetchall(self):
        return self._rows


class _Txn:
    def __init__(self, state):
        self.cur = _Cur(state)

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


@pytest.fixture
def fleet(monkeypatch):
    """Four approved items. `fail_ids` lists the items whose run fails."""
    state = {"items": [{"id": 10 + i, "position": i, "status": "approved",
                        "bundle_id": 5, "target_server_id": 100 + i}
                       for i in range(1, 5)],
             "fail_ids": {12}, "ran": [], "chains": 0, "dms": 0,
             "lock": threading.Lock()}

    def fetch_one(sql, params=None):
        if "FROM request_bundles" in sql:
            return {"kind": "fleet", "on_failure": state.get("on_failure", "stop")}
        if "FROM target_servers" in sql:
            return {"alias": f"srv{params[0] - 100}"}
        if "SELECT status FROM requests" in sql:
            it = next(i for i in state["items"] if i["id"] == params[0])
            return {"status": it["status"]}
        if "FROM requests" in sql:
            with state["lock"]:
                for it in sorted(state["items"], key=lambda i: i["position"]):
                    if it["status"] == "approved" and it["id"] not in params[1]:
                        return dict(it)
            return None
        raise AssertionError(sql)

    def run(req, client):
        it = next(i for i in state["items"] if i["id"] == req["id"])
        state["ran"].append(it["id"])
        it["status"] = "failed" if it["id"] in state["fail_ids"] else "completed"

    # An earlier test (a web lifespan shutdown) can leave the process draining.
    monkeypatch.setattr(lifecycle, "_draining", False)
    pool = _SyncPool()
    state["pool"] = pool
    monkeypatch.setattr(ex, "_pool", pool)
    monkeypatch.setattr(ex, "_run", run)
    monkeypatch.setattr(ex.db, "fetch_one", fetch_one)
    monkeypatch.setattr(ex.cfg, "get_int",
                        lambda key, default=None: state.get(key, default))
    monkeypatch.setattr(ex.db, "transaction", lambda: _Txn(state))
    monkeypatch.setattr(ex.audit, "log_in", lambda *a, **k: None)
    monkeypatch.setattr(bundles, "recompute_status", lambda cur, bid: "pending")
    monkeypatch.setattr(ex.notifications, "update_bundle_admin_dms",
                        lambda c, bid: state.__setitem__("dms", state["dms"] + 1))
    monkeypatch.setattr(ex, "_fleet_runners", {})
    monkeypatch.setattr(ex, "_fleet_taken", {})
    return state


def _row(state, i):
    return dict(state["items"][i])


def test_stop_fleet_runs_in_order_and_cancels_the_rest(fleet):
    for i in (3, 0, 2, 1):  # submit order must not matter
        ex.submit(_row(fleet, i), client=None)
    assert fleet["ran"] == [11, 12]
    st = {i["id"]: i["status"] for i in fleet["items"]}
    assert st == {11: "completed", 12: "failed", 13: "cancelled", 14: "cancelled"}
    assert fleet["items"][2]["reason"] == (
        "Not run: srv2 failed first. The fleet run stops at the first failure.")
    assert fleet["dms"] == 1
    assert all(c[0] == ex._run_fleet_chain for c in fleet["pool"].calls)


def test_continue_fleet_runs_every_item_after_a_failure(fleet):
    fleet["on_failure"] = "continue"
    for i in range(4):
        ex.submit(_row(fleet, i), client=None)
    assert all(c[0] == ex._run_fleet_chain for c in fleet["pool"].calls)
    assert sorted(fleet["ran"]) == [11, 12, 13, 14]
    assert not any(i["status"] == "cancelled" for i in fleet["items"])
    assert ex._fleet_runners == {} and ex._fleet_taken == {}


def test_continue_fleet_starts_at_most_the_cap_of_runners(fleet, monkeypatch):
    fleet["on_failure"] = "continue"
    pool = _HoldPool()
    monkeypatch.setattr(ex, "_pool", pool)
    for i in range(4):
        ex.submit(_row(fleet, i), client=None)
    assert len(pool.calls) == 2      # fleet_max_parallel default
    assert ex._fleet_runners == {5: 2}


@pytest.mark.parametrize("value, cap", [(None, 2), (1, 1), (0, 1), (10, 3)])
def test_fleet_max_parallel_leaves_one_worker_free(fleet, value, cap):
    if value is not None:
        fleet["fleet_max_parallel"] = value
    assert ex.fleet_max_parallel() == cap


def test_continue_fleet_never_runs_more_than_the_cap_at_once(fleet, monkeypatch):
    """A real pool of 4 workers, 8 items, cap 2: never more than 2 run at
    once, and each item runs exactly once."""
    from concurrent.futures import ThreadPoolExecutor
    fleet["on_failure"] = "continue"
    fleet["fail_ids"] = set()
    fleet["items"] = [{"id": 10 + i, "position": i, "status": "approved",
                       "bundle_id": 5, "target_server_id": 100 + i}
                      for i in range(1, 9)]
    live = {"now": 0, "max": 0}
    gate = threading.Lock()

    def run(req, client):
        with gate:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.02)
        with fleet["lock"]:
            it = next(i for i in fleet["items"] if i["id"] == req["id"])
            fleet["ran"].append(it["id"])
            it["status"] = "completed"
        with gate:
            live["now"] -= 1
    monkeypatch.setattr(ex, "_run", run)
    pool = ThreadPoolExecutor(max_workers=4)
    monkeypatch.setattr(ex, "_pool", pool)
    for i in range(8):
        ex.submit(_row(fleet, i), client=None)
    pool.shutdown(wait=True)
    assert live["max"] == 2
    assert sorted(fleet["ran"]) == list(range(11, 19))
    assert ex._fleet_runners == {} and ex._fleet_taken == {}


def test_second_submit_while_the_chain_runs_starts_no_second_chain(fleet, monkeypatch):
    pool = _HoldPool()
    monkeypatch.setattr(ex, "_pool", pool)
    ex.submit(_row(fleet, 0), client=None)
    ex.submit(_row(fleet, 1), client=None)
    assert len(pool.calls) == 1


def _claim_lost_env(monkeypatch, admin, kind, failed, mode="ro"):
    class T:
        enabled = True
        engine = "postgres"
        alias = "svc"
        id = 7
    monkeypatch.setattr(ex.targets, "get", lambda tid: T())
    monkeypatch.setattr(ex.engines, "is_executable", lambda e: True)
    rep = type("R", (), {"blocked": False, "blockers": [], "main_tier": "ro",
                         "statements": [1]})()
    monkeypatch.setattr(ex.query_safety, "analyze", lambda *a, **k: rep)
    monkeypatch.setattr(ex.admins, "is_admin", lambda uid: admin)
    monkeypatch.setattr(ex.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(ex.requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(ex.teams, "effective_mode_for_database", lambda *a, **k: mode)
    monkeypatch.setattr(ex.targets, "get_credentials", lambda *a, **k: ("u", "pw"))
    monkeypatch.setattr(ex.row_limits, "effective_caps", lambda uid: (1, 1))
    monkeypatch.setattr(ex, "bundle_run_info",
                        lambda r: {"kind": kind, "on_failure": "continue"})
    monkeypatch.setattr(ex, "_fail", lambda c, r, m: failed.append(m))
    monkeypatch.setattr(ex.db, "transaction", lambda: _Txn({"items": []}))
    monkeypatch.setattr(ex.db, "fetch_one", lambda *a, **k: None)


_REQ = {"id": 1, "target_server_id": 7, "database_name": "app", "query": "SELECT 1",
        "requester_slack_id": "U0EXAMPLE01", "bundle_id": 5}


def test_fleet_item_fails_when_requester_is_no_longer_admin(monkeypatch):
    failed = []
    _claim_lost_env(monkeypatch, admin=False, kind="fleet", failed=failed)
    ex._run(dict(_REQ), client=None)
    assert failed == ["Not run: the requester is no longer an admin. "
                      "A fleet run needs an admin."]


def test_plain_batch_item_skips_the_admin_check(monkeypatch):
    # The grant check that follows the admin check refuses the item. That
    # refusal proves the run passed the admin check without a stop.
    failed = []
    _claim_lost_env(monkeypatch, admin=False, kind="batch", failed=failed,
                    mode=None)
    ex._run(dict(_REQ), client=None)
    assert failed == ["Not run: your access to this database changed after "
                      "approval. If you still need this query, submit it again."]


def test_compact_fleet_card_for_sixty_servers(monkeypatch):
    monkeypatch.setattr(admins, "can_approve", lambda uid, it: True)
    items = [{"id": i, "position": i, "status": "pending", "target_server_id": i,
              "database_name": f"db{i}", "target_alias": f"server-alias-{i:03d}",
              "query": "SELECT 1", "required_tier": "ro", "engine": "postgres"}
             for i in range(1, 61)]
    bundle = {"id": 9, "requester_slack_id": "U0EXAMPLE01", "status": "pending",
              "justification": "check", "kind": "fleet", "on_failure": "stop"}
    blocks = notifications._build_bundle_dm_blocks(bundle, items, "U0EXAMPLE02")
    assert len(blocks) < 50
    assert blocks[0]["text"]["text"] == "Fleet run #9 · 60 servers · RO"
    text = "\n".join(b.get("text", {}).get("text", "") for b in blocks
                     if b["type"] == "section")
    assert text.count("SELECT 1") == 1
    for i in range(1, 61):
        assert f"server-alias-{i:03d} / db{i}" in text
    assert all(len(b["text"]["text"]) <= 2900 for b in blocks
               if b["type"] == "section")
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    ids = {e["action_id"] for e in actions[0]["elements"]}
    assert ids == {notifications.ACTION_BUNDLE_APPROVE_ALL,
                   notifications.ACTION_BUNDLE_REJECT_ALL}
    assert "Stops at the first failure" in str(blocks)


@pytest.mark.parametrize("handler", ["handle_approve", "handle_reject",
                                     "handle_request_changes"])
def test_slack_per_item_click_on_a_fleet_item_is_refused(monkeypatch, handler):
    sent = []
    monkeypatch.setattr(handlers, "_guard_admin", lambda *a, **k: True)
    monkeypatch.setattr(handlers.core_decide, "is_fleet_item", lambda rid: rid == 11)
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda c, uid, text, **k: sent.append(text))
    monkeypatch.setattr(handlers.core_decide, "decide",
                        lambda *a, **k: pytest.fail("decided a fleet item"))

    class Client:
        def views_open(self, **k):
            pytest.fail("opened a decision modal for a fleet item")
    acked = []
    body = {"user": {"id": "U0EXAMPLE02"}, "actions": [{"value": "11"}],
            "trigger_id": "t"}
    getattr(handlers, handler)(lambda: acked.append(1), body, Client())
    assert sent == ["Approve or reject the whole fleet run."]
    assert acked == [1]


@pytest.mark.parametrize("handler", ["handle_reject_submission",
                                     "handle_changes_submission"])
def test_slack_modal_submission_for_a_fleet_item_is_refused(monkeypatch, handler):
    from queryhub import core_decide
    sent = []
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda c, uid, text, **k: sent.append(text))

    def decide(*a, **k):
        raise core_decide.FleetItemRefused()
    monkeypatch.setattr(handlers.core_decide, "decide", decide)
    monkeypatch.setattr(handlers.core_decide, "apply_effects",
                        lambda *a, **k: pytest.fail("effects of a refusal"))
    body = {"user": {"id": "U0EXAMPLE02"},
            "view": {"private_metadata": "11", "state": {"values": {
                "reason_block": {"reason_input": {"value": "no"}}}}}}
    getattr(handlers, handler)(lambda: None, body, None)
    assert sent == ["Approve or reject the whole fleet run."]


def _bulk_env(monkeypatch, kind, in_scope):
    sent = []
    monkeypatch.setattr(handlers, "_guard_bundle", lambda *a, **k: True)
    monkeypatch.setattr(handlers.core_decide, "require_kill_switch_off", lambda: None)
    monkeypatch.setattr(handlers.db, "fetch_one", lambda sql, params=None: {"kind": kind})
    pending = [{"id": 11}, {"id": 12}]
    monkeypatch.setattr(handlers, "_bundle_pending_items", lambda bid: pending)
    monkeypatch.setattr(handlers.admins, "can_approve",
                        lambda uid, r: r["id"] in in_scope)
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda c, uid, text, **k: sent.append(text))
    return sent


def test_approve_all_refuses_a_fleet_run_partly_out_of_scope(monkeypatch):
    sent = _bulk_env(monkeypatch, "fleet", in_scope={11})
    monkeypatch.setattr(handlers.db, "transaction",
                        lambda: pytest.fail("approved part of a fleet run"))
    body = {"user": {"id": "U0EXAMPLE02"}, "actions": [{"value": "5"}]}
    handlers.handle_bundle_approve_all(lambda: None, body, None)
    assert sent == [":no_entry: Part of this fleet run is outside your approval "
                    "scope. An admin with the full scope must decide it."]


def test_reject_all_refuses_a_fleet_run_partly_out_of_scope(monkeypatch):
    sent = _bulk_env(monkeypatch, "fleet", in_scope={12})

    class Client:
        def views_open(self, **k):
            pytest.fail("opened the reject modal")
    body = {"user": {"id": "U0EXAMPLE02"}, "actions": [{"value": "5"}],
            "trigger_id": "t"}
    handlers.handle_bundle_reject_all(lambda: None, body, Client())
    assert len(sent) == 1 and "outside your approval scope" in sent[0]
    # The modal submission checks the rule again.
    sent.clear()
    monkeypatch.setattr(handlers.db, "transaction",
                        lambda: pytest.fail("rejected part of a fleet run"))
    view = {"private_metadata": "5", "state": {"values": {
        "reason_block": {"reason_input": {"value": "no"}}}}}
    handlers.handle_bundle_reject_submission(
        lambda: None, {"user": {"id": "U0EXAMPLE02"}, "view": view}, None)
    assert len(sent) == 1 and "outside your approval scope" in sent[0]


def test_partial_scope_rule_is_for_fleet_runs_only(monkeypatch):
    _bulk_env(monkeypatch, "batch", in_scope={11})
    assert handlers._refuse_partial_fleet(None, "U0EXAMPLE02", 5) is False
