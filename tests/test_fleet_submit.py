"""Fleet run: submit and approval guards (no live DB).

A fleet run is one SQL text sent to many (target, database) pairs as one
bundle, started by an admin, approved once, never auto-approved.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import auto_approve, bundles, core_submit as cs, executor, teams
from queryhub.web import routes_admin as ra, routes_queries as rq

ADMIN = "U0EXAMPLE001"
LEAD = "U0EXAMPLE002"        # approver only, not an admin
REQUESTER = "U0EXAMPLE003"
SUPER = "U0EXAMPLE004"
SQL = "SELECT count(*) FROM ledgers"


def _target(i):
    return SimpleNamespace(id=i, alias=f"example-{i}", enabled=True)


TARGETS = {f"example-{i}": _target(i) for i in range(1, 8)}


class _Cur:
    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return None


@pytest.fixture
def world(monkeypatch):
    st = {"insert": [], "notified": [], "dispatched": [], "decision_calls": 0,
          "modes": {}, "no_grant": set(), "admins": {ADMIN, SUPER},
          "supers": {SUPER}, "waiver": False}

    @contextlib.contextmanager
    def txn():
        yield _Cur()

    def insert(cur, **kw):
        st["insert"].append(kw)
        return {"bundle_id": 5,
                "item_rows": [{"id": 11 + i, "position": i + 1, "status": "pending"}
                              for i in range(len(kw["items"]))]}

    def decision(uid, mode, target, database, at_time=None):
        st["decision_calls"] += 1
        return {"id": 1, "max_tier": "ro"} if st["waiver"] else None

    def validated(uid, name, *, target_server_id, database_name, query, **kw):
        t = next(t for t in TARGETS.values() if t.id == target_server_id)
        return cs.Prepared(
            user_id=uid, user_name=name, target=t, database=database_name or "ledger",
            query=query, required_mode=st["modes"].get(t.id, "ro"), justification=None,
            wants_result=True, result_format="csv", sched_for=None, explain_plan=None,
            risk_summary=None, origin="web")

    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq.deps, "block_if_password_change_required", lambda claims: None)
    monkeypatch.setattr(rq.deps, "employment_checked", lambda claims: False)
    monkeypatch.setattr(rq, "_client_ctx", lambda request: (None, None))
    monkeypatch.setattr(rq, "_bot_client", lambda: None)
    monkeypatch.setattr(rq, "_target_by_alias", lambda alias: TARGETS.get(alias))
    monkeypatch.setattr(rq.db, "transaction", txn)
    monkeypatch.setattr(rq.audit_mod, "log_in", lambda *a, **k: None)
    monkeypatch.setattr(rq.admins, "is_admin", lambda uid: uid in st["admins"])
    monkeypatch.setattr(rq.admins, "is_super_admin", lambda uid: uid in st["supers"])
    monkeypatch.setattr(rq.admins, "list_active", lambda: [{"slack_user_id": ADMIN}])
    monkeypatch.setattr(teams, "effective_grant_for_user",
                        lambda uid, tid: None if tid in st["no_grant"]
                        else {"allowed_databases": None, "mode": "rw"})
    monkeypatch.setattr(rq.core_submit, "validate_submission", validated)
    monkeypatch.setattr(auto_approve, "decision", decision)
    monkeypatch.setattr(bundles, "is_enabled", lambda: True)
    monkeypatch.setattr(bundles, "max_items", lambda: 5)
    monkeypatch.setattr(bundles, "fleet_max_targets", lambda: 6, raising=False)
    monkeypatch.setattr(bundles, "insert_bundle_with_items", insert)
    monkeypatch.setattr(executor, "submit",
                        lambda row, client: st["dispatched"].append(row["id"]))
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "notify_admins_bundle",
                        lambda client, bid: st["notified"].append(bid))
    return st


def _body(n=2, sqls=None, **kw):
    items = [rq.BatchItemIn(connectionId=f"example-{i}", databaseId="ledger",
                            sql=(sqls[i - 1] if sqls else SQL))
             for i in range(1, n + 1)]
    return rq.BatchIn(items=items, **kw)


def _submit(body, uid=ADMIN):
    return rq.submit_batch(body, request=None, claims={"sub": uid, "name": "Ex"})


def _status(exc):
    return exc.value.status_code, exc.value.detail


# ---- who ---------------------------------------------------------------------

def test_fleet_refuses_a_requester(world):
    with pytest.raises(HTTPException) as e:
        _submit(_body(fleet=True), REQUESTER)
    assert _status(e)[0] == 403 and _status(e)[1]["code"] == "fleet_not_allowed"


def test_fleet_refuses_an_approver_only_person(world):
    with pytest.raises(HTTPException) as e:
        _submit(_body(fleet=True), LEAD)
    assert _status(e)[0] == 403 and _status(e)[1]["code"] == "fleet_not_allowed"
    assert world["insert"] == []


def test_fleet_accepts_an_admin(world):
    out = _submit(_body(fleet=True))
    assert out["fleet"] is True and len(out["items"]) == 2
    assert world["notified"] == [5]


# ---- shape -------------------------------------------------------------------

def test_fleet_refuses_different_sql_texts(world):
    with pytest.raises(HTTPException) as e:
        _submit(_body(fleet=True, sqls=[SQL, SQL + " WHERE 1 = 1"]))
    assert _status(e)[0] == 422


def test_fleet_ignores_trailing_whitespace_when_comparing_sql(world):
    _submit(_body(fleet=True, sqls=[SQL, SQL + "  \n"]))
    assert len(world["insert"]) == 1


def test_fleet_refuses_a_duplicate_pair(world):
    body = rq.BatchIn(fleet=True, items=[
        rq.BatchItemIn(connectionId="example-1", databaseId="ledger", sql=SQL),
        rq.BatchItemIn(connectionId="example-1", databaseId="ledger", sql=SQL)])
    with pytest.raises(HTTPException) as e:
        _submit(body)
    assert _status(e)[0] == 422


def test_fleet_uses_its_own_cap(world):
    _submit(_body(n=6, fleet=True))          # 6 > batch cap 5, = fleet cap 6
    with pytest.raises(HTTPException) as e:
        _submit(_body(n=7, fleet=True))
    assert _status(e)[0] == 422


# ---- missing grants ----------------------------------------------------------

def test_two_missing_grants_give_one_409_naming_both(world):
    world["no_grant"] = {2, 3}
    with pytest.raises(HTTPException) as e:
        _submit(_body(n=4, fleet=True))
    status, detail = _status(e)
    assert status == 409 and detail["code"] == "fleet_refused"
    assert "example-2" in detail["message"] and "example-3" in detail["message"]
    assert "example-1" not in detail["message"]
    assert world["insert"] == []


def test_refusal_names_at_most_twenty_pairs():
    err = rq._fleet_refusal([f"p{i} / db: no grant" for i in range(25)])
    msg = err.detail["message"]
    assert "p19 /" in msg and "p20 /" not in msg and msg.endswith("and 5 more")


# ---- no auto-approve ---------------------------------------------------------

@pytest.mark.parametrize("uid", [ADMIN, SUPER])
def test_fleet_never_auto_approves(world, uid):
    world["waiver"] = True
    out = _submit(_body(fleet=True), uid)
    assert world["decision_calls"] == 0
    assert world["dispatched"] == []
    assert {i["status"] for i in out["items"]} == {rq.mapping.status_to_web("pending")}
    assert world["notified"] == [5]


# ---- on failure --------------------------------------------------------------

def test_default_on_failure_is_continue_when_all_ro(world):
    out = _submit(_body(fleet=True))
    assert out["onFailure"] == "continue"
    assert world["insert"][0]["on_failure"] == "continue"


def test_default_on_failure_is_stop_with_one_rw(world):
    world["modes"] = {2: "rw"}
    out = _submit(_body(fleet=True))
    assert out["onFailure"] == "stop"


def test_on_failure_override_is_respected(world):
    world["modes"] = {2: "rw"}
    assert _submit(_body(fleet=True, onFailure="continue"))["onFailure"] == "continue"
    assert _submit(_body(fleet=True, onFailure="stop"))["onFailure"] == "stop"


def test_on_failure_refuses_an_unknown_value(world):
    with pytest.raises(HTTPException) as e:
        _submit(_body(fleet=True, onFailure="ignore"))
    assert _status(e)[0] == 422


def test_insert_is_called_with_kind_fleet(world):
    _submit(_body(fleet=True))
    assert world["insert"][0]["kind"] == "fleet"


# ---- one approval ------------------------------------------------------------

def test_core_decide_refuses_a_fleet_item_and_writes_nothing(monkeypatch):
    from queryhub import core_decide
    monkeypatch.setattr(core_decide.db, "fetch_one",
                        lambda sql, params=None: {"kind": "fleet"}
                        if "request_bundles" in sql else pytest.fail(sql))
    monkeypatch.setattr(core_decide.db, "transaction",
                        lambda: pytest.fail("a fleet item opened a transaction"))
    for decision, reason in (("approve", None), ("reject", "no"),
                             ("changes", "fix it")):
        with pytest.raises(core_decide.FleetItemRefused) as e:
            core_decide.decide(11, decision, by_id=ADMIN, by_name="Ex",
                               reason=reason)
        assert e.value.code == "fleet_single_item"


def test_core_decide_lets_a_plain_batch_item_through(monkeypatch):
    from queryhub import core_decide
    monkeypatch.setattr(core_decide.db, "fetch_one",
                        lambda sql, params=None: {"kind": "batch"}
                        if "request_bundles" in sql else None)
    monkeypatch.setattr(core_decide, "require_kill_switch_off", lambda: None)

    class _T:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(core_decide.db, "transaction", lambda: _T())
    # Not pending any more: decide returns None, and it raised nothing.
    assert core_decide.decide(11, "approve", by_id=ADMIN, by_name="Ex") is None


def _refusing_decide(*a, **k):
    from queryhub import core_decide
    raise core_decide.FleetItemRefused()


def test_single_item_decision_on_a_fleet_item_is_refused(monkeypatch):
    row = {"id": 11, "query": SQL, "target_server_id": 1, "requester_slack_id": ADMIN,
           "status": "pending", "engine": "postgres", "required_tier": "ro"}
    monkeypatch.setattr(ra.db, "fetch_one", lambda sql, params=None: row)
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need="review", **k: ADMIN)
    monkeypatch.setattr(ra.core_decide, "decide", _refusing_decide)
    monkeypatch.setattr(ra.core_decide, "apply_effects",
                        lambda *a, **k: pytest.fail("effects of a refusal"))
    with pytest.raises(HTTPException) as e:
        ra.admin_decision(11, ra.DecisionIn(decision="approve"),
                          claims={"sub": ADMIN, "name": "Ex"})
    assert _status(e)[0] == 409 and _status(e)[1]["code"] == "fleet_single_item"


def test_batch_approve_skips_a_fleet_item(monkeypatch):
    rows = {i: {"id": i, "query": SQL, "target_server_id": 1,
                "requester_slack_id": ADMIN, "status": "pending",
                "engine": "postgres", "required_tier": "ro"} for i in (11, 12)}
    monkeypatch.setattr(ra.db, "fetch_one", lambda sql, params=None: rows[params[0]])
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need="review", **k: ADMIN)
    monkeypatch.setattr(ra.admins, "can_approve", lambda uid, row: True)
    monkeypatch.setattr(ra.core_decide, "require_kill_switch_off", lambda: None)
    monkeypatch.setattr(ra, "_slack_client", lambda: None)
    decided = []

    def decide(rid, *a, **k):
        if rid == 11:
            _refusing_decide()
        decided.append(rid)
        return None
    monkeypatch.setattr(ra.core_decide, "decide", decide)
    out = ra.admin_batch_approve(ra.BatchIn(ids=["11", "12"]),
                                 claims={"sub": ADMIN, "name": "Ex"})
    assert out == {"approved": 0}
    assert decided == [12]


# ---- a plain batch is unchanged ----------------------------------------------

def test_a_plain_batch_is_unchanged(world):
    world["supers"] = {SUPER}
    out = _submit(_body(n=2), SUPER)
    assert "fleet" not in out and "onFailure" not in out
    assert world["insert"][0]["kind"] == "batch"
    assert world["insert"][0]["on_failure"] is None
    # super-admin auto-approval still applies to a plain batch
    assert world["dispatched"] == [11, 12]
    # and the plain cap still holds
    with pytest.raises(HTTPException) as e:
        _submit(_body(n=6), SUPER)
    assert _status(e)[0] == 422
