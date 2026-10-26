"""The approval routes admit an approver, not only an admin.

A pod lead holds an `approver` role and no `admin` role. Slack lets that person
approve the requests of their own servers. The web routes and the IdP portal,
which forwards to them, refused the same person with a 403, because the gate
asked for an `admin` role. The rule is now: whoever may approve in Slack may
approve here, whatever the role.

The new `approve` level opens the DOOR of four routes: the queue, one decision,
a bundle decision and batch approve. What each request admits is still
`can_approve`, per request, and these tests hold both halves:

1. the gate: who passes `approve`, and that no other level got wider;
2. the four routes, and only those four, take the level;
3. an approver reaches an in-scope request and is refused an out-of-scope one,
   on each route.

The callers are role rows, the shape `access.roles` returns, with
`access_model_v2` on, as in production.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import access, admins, teams
from queryhub.web import admin as web_admin
from queryhub.web import routes_admin as ra

ROUTES_ADMIN = Path(ra.__file__)

SUPER = "U0EXAMPLE001"       # admin everywhere
POD_LEAD = "U0EXAMPLE006"    # approver on one target, up to RO
FLEET_LEAD = "U0EXAMPLE007"  # approver everywhere
REQUESTER = "U0EXAMPLE008"   # no role at all

IN_SCOPE_TARGET = 42
OUT_OF_SCOPE_TARGET = 99


def _role(role="admin", **over) -> dict:
    row = {"role": role, "scope_team_id": None, "all_teams": True,
           "scope_target_id": None, "all_targets": True, "max_tier": None,
           "any_tier": True, "valid_until": None}
    row.update(over)
    return row


PERSONAS = {
    SUPER: [_role()],
    POD_LEAD: [_role("approver", all_targets=False,
                     scope_target_id=IN_SCOPE_TARGET,
                     max_tier="ro", any_tier=False)],
    FLEET_LEAD: [_role("approver")],
    REQUESTER: [],
}

NEED_APPROVE = "Refused: you need admin or approver access."
NEED_ADMIN = "Refused: you need admin access."
NEED_DBA = "Refused: you need DBA access."


@pytest.fixture
def personas(monkeypatch):
    monkeypatch.setattr(teams, "use_v2", lambda: True)
    monkeypatch.setattr(access, "roles",
                        lambda pid: [dict(r) for r in PERSONAS.get(pid, [])])


def _refusal(uid, level, **kw):
    with pytest.raises(HTTPException) as e:
        web_admin.require_admin({"sub": uid}, level, **kw)
    assert e.value.status_code == 403
    return e.value.detail["message"]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uid", [SUPER, POD_LEAD, FLEET_LEAD])
def test_the_approve_level_admits_an_admin_and_an_approver(personas, uid):
    assert web_admin.require_admin({"sub": uid}, "approve") == uid


def test_the_approve_level_refuses_a_person_with_no_role(personas):
    assert _refusal(REQUESTER, "approve") == NEED_APPROVE


@pytest.mark.parametrize("level", ["review", "dba", "access"])
@pytest.mark.parametrize("uid", [POD_LEAD, FLEET_LEAD])
def test_no_other_level_got_wider(personas, level, uid):
    """An approver still reaches no other admin route. Only the four approval
    routes take `approve`."""
    assert _refusal(uid, level) == NEED_ADMIN


def test_a_request_is_still_checked_against_the_approvers_scope(personas):
    """Passing the door is not authority over a request. `request=` runs
    `can_approve`, which holds the target, the tier ceiling and the rule that
    nobody decides their own request."""
    own_target = {"tier": "ro", "required_tier": "ro",
                  "target_server_id": IN_SCOPE_TARGET,
                  "requester_slack_id": "U0SOMEONE"}
    assert web_admin.require_admin({"sub": POD_LEAD}, "approve",
                                   request=own_target) == POD_LEAD
    other_target = dict(own_target, target_server_id=OUT_OF_SCOPE_TARGET)
    assert _refusal(POD_LEAD, "approve", request=other_target) == (
        "Refused: this query is outside your approval scope.")
    rw = dict(own_target, tier="rw", required_tier="rw")
    assert _refusal(POD_LEAD, "approve", request=rw) == (
        "Refused: this query is outside your approval scope.")
    own = dict(own_target, requester_slack_id=POD_LEAD)
    assert _refusal(POD_LEAD, "approve", request=own) == (
        "Refused: this query is outside your approval scope.")


def test_the_old_model_has_no_approver_role_so_the_door_is_is_admin(monkeypatch):
    monkeypatch.setattr(teams, "use_v2", lambda: False)
    monkeypatch.setattr(admins, "is_admin", lambda pid: pid == SUPER)
    assert admins.holds_approval_role(SUPER)
    assert not admins.holds_approval_role(POD_LEAD)


def test_an_unknown_level_still_fails_closed(personas):
    with pytest.raises(ValueError):
        web_admin.require_admin({"sub": SUPER}, "approver")


# ---------------------------------------------------------------------------
# Which routes
# ---------------------------------------------------------------------------

APPROVE_ROUTES = {
    "admin_queue": ("GET", "/queue"),
    "admin_decision": ("POST", "/queue/{request_id}/decision"),
    "admin_batch_approve": ("POST", "/queue/batch-approve"),
    "admin_bundle_decision": ("POST", "/queue/bundles/{bundle_id}/decision"),
}


def _levels() -> dict[str, list[str]]:
    tree = ast.parse(ROUTES_ADMIN.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        for call in ast.walk(fn):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "require_admin"):
                continue
            level = "review"
            if len(call.args) > 1:
                level = getattr(call.args[1], "value", call.args[1])
            out.setdefault(fn.name, []).append(level)
    return out


def test_exactly_four_routes_take_the_approve_level():
    """A fifth route on this level is a decision. Make it here, on purpose."""
    found = {name for name, levels in _levels().items() if "approve" in levels}
    assert found == set(APPROVE_ROUTES)
    paths = {(m, route.path.removeprefix("/api/admin"))
             for route in ra.router.routes
             if getattr(route, "endpoint", None) is not None
             and route.endpoint.__name__ in APPROVE_ROUTES
             for m in route.methods - {"HEAD", "OPTIONS"}}
    assert paths == set(APPROVE_ROUTES.values())


@pytest.mark.parametrize("name", sorted(APPROVE_ROUTES))
def test_an_approval_route_takes_no_other_level(name):
    assert set(_levels()[name]) == {"approve"}, name


# ---------------------------------------------------------------------------
# The routes, called by an approver
# ---------------------------------------------------------------------------

def _row(rid, target):
    return {"id": rid, "tier": "ro", "required_tier": "ro",
            "target_server_id": target, "requester_slack_id": "U0SOMEONE",
            "status": "pending"}


ROWS = {1: _row(1, IN_SCOPE_TARGET), 2: _row(2, OUT_OF_SCOPE_TARGET)}


@pytest.fixture
def world(monkeypatch, personas):
    """Two pending requests: one on the lead's target, one elsewhere."""
    monkeypatch.setattr(ra.db, "fetch_all", lambda *a, **k: list(ROWS.values()))
    monkeypatch.setattr(ra.db, "fetch_one",
                        lambda sql, params=None: ROWS.get(params[0]))
    # `can_approve` is the real one, over the persona's rows.
    monkeypatch.setattr(ra.people, "namer", lambda names: (lambda x: x))
    monkeypatch.setattr(ra.pending_expiry, "window", lambda: None)
    monkeypatch.setattr(ra.mapping, "queue_item",
                        lambda r, *a, **k: {"id": str(r["id"]),
                                            "escalate": False})
    monkeypatch.setattr(ra.core_decide, "require_kill_switch_off", lambda: None)
    decided = []

    def decide(rid, decision, **kw):
        decided.append((rid, decision, kw["by_id"]))
        return SimpleNamespace(row={"status": "approved"})

    monkeypatch.setattr(ra.core_decide, "decide", decide)
    monkeypatch.setattr(ra.core_decide, "apply_effects", lambda *a, **k: None)
    monkeypatch.setattr(ra, "_slack_client", lambda: None)
    monkeypatch.setattr(ra.mapping, "status_to_web", lambda s: s)
    return decided


def test_the_queue_lists_only_the_requests_of_the_approvers_scope(world):
    got = ra.admin_queue(claims={"sub": POD_LEAD})
    assert [i["id"] for i in got["queue"]] == ["1"]


def test_the_queue_of_a_fleet_approver_lists_every_request(world):
    got = ra.admin_queue(claims={"sub": FLEET_LEAD})
    assert [i["id"] for i in got["queue"]] == ["1", "2"]


def test_one_decision_in_scope_goes_through(world):
    out = ra.admin_decision(1, ra.DecisionIn(decision="approve"),
                            claims={"sub": POD_LEAD})
    assert out == {"id": "1", "status": "approved"}
    assert world == [(1, "approve", POD_LEAD)]


def test_one_decision_out_of_scope_is_refused_and_decides_nothing(world):
    with pytest.raises(HTTPException) as e:
        ra.admin_decision(2, ra.DecisionIn(decision="approve"),
                          claims={"sub": POD_LEAD})
    assert e.value.status_code == 403
    assert world == []


def test_batch_approve_skips_what_is_outside_the_approvers_scope(world):
    out = ra.admin_batch_approve(ra.BatchIn(ids=["1", "2"]),
                                 claims={"sub": POD_LEAD})
    assert out == {"approved": 1}
    assert world == [(1, "approve", POD_LEAD)]


def test_a_bundle_decision_passes_the_scope_check_to_the_decision(world, monkeypatch):
    seen = []

    def decide_bundle(bundle_id, decision, *, may_decide, **kw):
        seen.extend([may_decide(ROWS[1]), may_decide(ROWS[2])])
        return []

    monkeypatch.setattr(ra.core_decide, "decide_bundle", decide_bundle)
    try:
        ra.admin_bundle_decision(5, ra.DecisionIn(decision="approve"),
                                 claims={"sub": POD_LEAD})
    except HTTPException:
        pass   # what the route does with an empty outcome is not under test
    assert seen == [True, False]


@pytest.mark.parametrize("call", [
    lambda: ra.admin_queue(claims={"sub": REQUESTER}),
    lambda: ra.admin_decision(1, ra.DecisionIn(decision="approve"),
                              claims={"sub": REQUESTER}),
    lambda: ra.admin_batch_approve(ra.BatchIn(ids=["1"]),
                                   claims={"sub": REQUESTER}),
    lambda: ra.admin_bundle_decision(5, ra.DecisionIn(decision="approve"),
                                     claims={"sub": REQUESTER}),
], ids=["queue", "decision", "batch", "bundle"])
def test_a_person_with_no_role_is_refused_before_any_work(world, call):
    with pytest.raises(HTTPException) as e:
        call()
    assert e.value.status_code == 403
    assert world == []
