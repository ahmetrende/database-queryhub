"""One decision for a whole batch: every open item, or none (PLA-1592 U12).

Rejecting a batch from the web took one call per item. A call that failed half
way left the batch half rejected, and nothing could undo the half that landed.
`core_decide.decide_bundle` locks the items, checks all of them, and only then
writes, all on one cursor of one transaction. These tests drive it with a fake
cursor. tests/test_integration_queue_metrics.py runs it on a real database,
where the rollback is PostgreSQL's own.
"""
from datetime import datetime, timedelta, timezone

import pytest

from queryhub import core_decide
from queryhub.web import routes_admin as ra

REQUESTER = "U0EXAMPLE001"
DBA = "U0EXAMPLE002"


def _item(i, status="pending", decided_by=None, decided_name=None, sched=None):
    return {"id": i, "status": status, "scheduled_for": sched,
            "requester_slack_id": REQUESTER, "decided_by_slack_id": decided_by,
            "decided_by_name": decided_name, "query": "UPDATE t SET a = 1 WHERE id = 1",
            "target_server_id": 3, "engine": "postgres", "required_tier": "rw"}


class _Cur:
    """The lock SELECT answers `items`. Each UPDATE answers the row it moved,
    unless its id is in `lost` (a row that was not pending any more)."""

    def __init__(self, items, lost=()):
        self.items = items
        self.lost = set(lost)
        self.executed = []
        self._one = None

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        if flat.startswith("UPDATE requests"):
            rid = params[-1]
            status = params[0]
            self._one = None if rid in self.lost else {
                "id": rid, "status": status, "bundle_id": 311,
                "requester_slack_id": REQUESTER, "decided_by_slack_id": DBA,
                "decision_reason": params[1] if status != "approved" else None}

    def fetchall(self):
        return list(self.items)

    def fetchone(self):
        return self._one


class _Txn:
    def __init__(self, cur, log):
        self.cur, self.log = cur, log

    def __enter__(self):
        return self.cur

    def __exit__(self, exc_type, *a):
        self.log.append("rollback" if exc_type else "commit")
        return False


@pytest.fixture
def wired(monkeypatch):
    state = {"audit": [], "txn": []}

    def install(items, lost=()):
        cur = _Cur(items, lost)
        state["cur"] = cur
        monkeypatch.setattr(core_decide.db, "transaction", lambda: _Txn(cur, state["txn"]))
        return cur
    monkeypatch.setattr(core_decide.audit, "log_in",
                        lambda c, rid, bid, bname, action, details=None:
                        state["audit"].append((rid, action, details)))
    state["install"] = install
    return state


def _updates(cur):
    return [(sql, p) for sql, p in cur.executed if sql.startswith("UPDATE requests")]


def _decide(decision="reject", reason="the batch needs a WHERE clause", allow=None):
    return core_decide.decide_bundle(
        311, decision, by_id=DBA, by_name="Sam Archer", reason=reason,
        may_decide=allow or (lambda row: True))


# ---- the happy path ------------------------------------------------------------

def test_every_open_item_is_decided_with_its_own_audit_row(wired):
    cur = wired["install"]([_item(2045), _item(2046)])
    out = _decide()
    assert [o.row["id"] for o in out] == [2045, 2046]
    assert all(o.decision == "reject" for o in out)
    ups = _updates(cur)
    assert len(ups) == 2
    # The same guarded write as a single decision: only a pending row moves.
    assert all("AND status = 'pending'" in sql for sql, _ in ups)
    assert wired["audit"] == [
        (2045, "rejected", {"reason": "the batch needs a WHERE clause",
                            "bulk": True, "bundle_id": 311}),
        (2046, "rejected", {"reason": "the batch needs a WHERE clause",
                            "bulk": True, "bundle_id": 311}),
    ]
    assert wired["txn"] == ["commit"]


def test_the_items_are_locked_before_anything_is_checked(wired):
    cur = wired["install"]([_item(2045)])
    _decide()
    lock_sql, params = cur.executed[0]
    assert "FOR UPDATE" in lock_sql and "WHERE bundle_id = %s" in lock_sql
    assert params == (311,)


def test_changes_writes_changes_requested(wired):
    wired["install"]([_item(2045)])
    out = _decide("changes", "split it in two")
    assert out[0].decision == "changes"
    assert wired["audit"][0][1] == "changes_requested"


def test_approve_of_a_scheduled_batch_parks_every_item(wired):
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    cur = wired["install"]([_item(2045, sched=later), _item(2046, sched=later)])
    out = _decide("approve", None)
    assert [o.deferred for o in out] == [True, True]
    assert [p[0] for _, p in _updates(cur)] == ["scheduled", "scheduled"]
    assert wired["audit"][0][2] == {"scheduled_for": str(later), "deferred": True,
                                     "bulk": True, "bundle_id": 311}


def test_an_item_approved_when_it_was_sent_is_not_part_of_the_decision(wired):
    """A waiver approved it at submit. It never waited in the queue, so it is
    no conflict and the decision does not touch it."""
    cur = wired["install"]([_item(2044, "completed", "AUTO", "auto-approved (grant #9)"),
                            _item(2045)])
    out = _decide()
    assert [o.row["id"] for o in out] == [2045]
    assert [p[-1] for _, p in _updates(cur)] == [2045]


def test_a_super_admin_approval_in_an_old_row_is_settled_too(wired):
    """Before 2026-10-22 such a row named the requester as the approver."""
    cur = wired["install"]([
        _item(2044, "completed", REQUESTER, "Jordan Ray (super-admin)"), _item(2045)])
    _decide()
    assert [p[-1] for _, p in _updates(cur)] == [2045]


# ---- refusals write nothing ------------------------------------------------------

@pytest.mark.parametrize("taken", [
    _item(2045, "approved", DBA, "Sam Archer"),          # approved from Slack
    _item(2045, "rejected", DBA, "Sam Archer"),
    _item(2045, "changes_requested", DBA, "Sam Archer"),
    _item(2045, "cancelled"),                            # withdrawn or expired
    # A person who approved their own request by hand is not the old
    # super-admin row: no label, so it is a decision from somewhere else.
    _item(2045, "approved", REQUESTER, "Jordan Ray"),
])
def test_an_item_decided_elsewhere_refuses_the_whole_batch(wired, taken):
    cur = wired["install"]([taken, _item(2046)])
    with pytest.raises(core_decide.BundleConflict) as e:
        _decide()
    assert e.value.item_ids == [2045]
    assert _updates(cur) == [] and wired["audit"] == []
    assert wired["txn"] == ["rollback"]


def test_a_batch_with_nothing_left_to_decide_is_a_conflict(wired):
    cur = wired["install"]([_item(2044, "completed", "AUTO", "auto-approved")])
    with pytest.raises(core_decide.BundleConflict) as e:
        _decide()
    assert e.value.item_ids == []
    assert _updates(cur) == []


def test_an_unknown_batch_is_not_found(wired):
    wired["install"]([])
    with pytest.raises(core_decide.BundleNotFound):
        _decide()


def test_one_item_outside_the_scope_refuses_the_whole_batch(wired):
    cur = wired["install"]([_item(2045), _item(2046)])
    with pytest.raises(core_decide.BundleOutOfScope) as e:
        _decide(allow=lambda row: row["id"] != 2046)
    assert e.value.item_ids == [2046]
    assert _updates(cur) == [] and wired["audit"] == []


def test_the_scope_check_sees_what_can_approve_reads(wired):
    """A tier ceiling reads `required_tier` and `engine`. A row without them
    admitted nothing once (see access.can_approve), so the lock reads them."""
    wired["install"]([_item(2045)])
    seen = []
    _decide(allow=lambda row: seen.append(row) or True)
    for col in ("query", "target_server_id", "requester_slack_id",
                "required_tier", "engine"):
        assert col in seen[0], col
        assert col in core_decide._BUNDLE_COLS, col


def test_an_auto_approved_item_outside_the_scope_does_not_block(wired):
    """A waiver approved it, so the caller's scope is not asked about it."""
    wired["install"]([_item(2044, "completed", "AUTO", "auto-approved"), _item(2045)])
    out = _decide(allow=lambda row: row["id"] == 2045)
    assert [o.row["id"] for o in out] == [2045]


def test_a_write_that_finds_no_pending_row_rolls_everything_back(wired):
    """Cannot happen under the lock. If it does, the first item's write must
    not stay: the transaction raises and rolls back."""
    wired["install"]([_item(2045), _item(2046)], lost={2046})
    with pytest.raises(core_decide.BundleConflict) as e:
        _decide()
    assert e.value.item_ids == [2046]
    assert wired["txn"] == ["rollback"]


@pytest.mark.parametrize("decision,reason", [("delete", "x"), ("reject", " "),
                                             ("changes", None)])
def test_bad_input_is_refused_before_the_transaction(wired, decision, reason):
    wired["install"]([_item(2045)])
    with pytest.raises(ValueError):
        _decide(decision, reason)
    assert wired["txn"] == []


# ---- the route -----------------------------------------------------------------

@pytest.fixture
def route(monkeypatch):
    seen = {"effects": [], "decide": None}
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need="review", **k: DBA)
    monkeypatch.setattr(ra, "_slack_client", lambda: None)

    def fake_decide(bundle_id, decision, **kw):
        seen["decide"] = (bundle_id, decision, kw)
        if seen.get("raise"):
            raise seen["raise"]
        return [core_decide.Outcome(row={"id": 2045, "status": "rejected"},
                                    decision=decision, deferred=False),
                core_decide.Outcome(row={"id": 2046, "status": "rejected"},
                                    decision=decision, deferred=False)]
    monkeypatch.setattr(ra.core_decide, "decide_bundle", fake_decide)
    monkeypatch.setattr(ra.core_decide, "apply_effects",
                        lambda client, o: seen["effects"].append(o.row["id"]))
    return seen


def _post(decision="reject", note="needs a WHERE clause"):
    return ra.admin_bundle_decision(311, ra.DecisionIn(decision=decision, note=note),
                                    claims={"sub": DBA, "name": "Sam Archer"})


def test_the_route_answers_every_item_and_notifies_each(route):
    out = _post()
    assert out == {"bundleId": "311", "decision": "reject",
                   "items": [{"id": "2045", "status": "rejected"},
                             {"id": "2046", "status": "rejected"}]}
    assert route["effects"] == [2045, 2046]
    bundle_id, decision, kw = route["decide"]
    assert (bundle_id, decision, kw["reason"], kw["by_id"]) == (
        311, "reject", "needs a WHERE clause", DBA)


def test_the_scope_check_is_can_approve_for_the_caller(route, monkeypatch):
    asked = []
    monkeypatch.setattr(ra.admins, "can_approve",
                        lambda uid, row: asked.append((uid, row["id"])) or False)
    _post()
    may = route["decide"][2]["may_decide"]
    assert may({"id": 2045}) is False
    assert asked == [(DBA, 2045)]


def test_one_failed_notification_does_not_stop_the_next_item(route, monkeypatch):
    """After the commit, a Slack error on the first item must not leave the
    second approved and never run."""
    done = []

    def effects(client, o):
        if o.row["id"] == 2045:
            raise RuntimeError("Slack is down")
        done.append(o.row["id"])
    monkeypatch.setattr(ra.core_decide, "apply_effects", effects)
    assert len(_post()["items"]) == 2
    assert done == [2046]


@pytest.mark.parametrize("exc,status,code", [
    (core_decide.BundleNotFound("x"), 404, "not_found"),
    (core_decide.BundleOutOfScope("x", [2046]), 403, "forbidden"),
    (core_decide.BundleConflict("x", [2045]), 409, "conflict"),
])
def test_a_refusal_maps_to_its_status_and_notifies_nobody(route, exc, status, code):
    route["raise"] = exc
    with pytest.raises(Exception) as e:
        _post()
    assert e.value.status_code == status
    assert e.value.detail["code"] == code
    assert route["effects"] == []


def test_the_conflict_names_the_items_that_already_have_a_decision(route):
    route["raise"] = core_decide.BundleConflict("x", [2045])
    with pytest.raises(Exception) as e:
        _post()
    assert e.value.detail["items"] == ["2045"]


@pytest.mark.parametrize("decision,note", [("delete", "x"), ("reject", ""),
                                           ("changes", "   ")])
def test_the_route_refuses_bad_input_before_deciding(route, decision, note):
    with pytest.raises(Exception) as e:
        _post(decision, note)
    assert e.value.status_code == 400
    assert route["decide"] is None
