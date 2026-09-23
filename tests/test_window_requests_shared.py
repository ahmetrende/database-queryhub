"""Asking for a read-only auto-approve window: one rulebook, two surfaces.

It was Slack-only, with its rules inline in the modal handler. The web form
needs the same rules, and a second copy is a place for them to drift -- so the
rules moved to `auto_approve_requests.submit_window`, the Slack handler maps
its refusals onto the modal's fields, and the web route onto HTTP statuses.
Granting stays a DBA's decision on both: the same approve/reject card goes to
every admin in Slack.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import auto_approve, auto_approve_requests as aar, core_submit, targets, teams

UID = "U0EXAMPLE001"
T = SimpleNamespace(id=53, alias="prod-ledger")


@pytest.fixture
def core(monkeypatch):
    st = {"kill": False, "target": True, "db": True, "pending": None, "gone": False,
          "created": {"id": 7, "requester_slack_id": UID, "status": "pending",
                      "database_name": None, "window_minutes": 60, "max_tier": "ro"},
          "made": [], "held": "rw"}
    monkeypatch.setattr(core_submit, "kill_switch_on", lambda: st["kill"])
    monkeypatch.setattr(core_submit, "kill_switch_message", lambda: "halted")
    monkeypatch.setattr(teams, "can_use_target", lambda pid, tid: st["target"])
    monkeypatch.setattr(teams, "can_use_database", lambda pid, tid, d: st["db"])
    monkeypatch.setattr(teams, "effective_mode_for_database", lambda pid, tid, d: st["held"])
    monkeypatch.setattr(teams, "effective_grant_for_user", lambda pid, tid: {"mode": st["held"]})
    monkeypatch.setattr(auto_approve, "validate_scope", lambda tid, d: None)
    monkeypatch.setattr(aar, "find_pending_for", lambda pid, tid: st["pending"])
    monkeypatch.setattr(targets, "get", lambda tid: None if st["gone"] else T)

    def create(**kw):
        st["made"].append(kw)
        return st["created"]
    monkeypatch.setattr(aar, "create", create)
    return st


def _submit(**kw):
    args = dict(principal_id=UID, name="Example", target_id=53, window_minutes=60,
                reason="reading the ledger for a report")
    args.update(kw)
    return aar.submit_window(**args)


def test_a_good_request_is_filed_read_only(core):
    row, t = _submit()
    assert row["id"] == 7 and t is T
    assert core["made"][0]["max_tier"] == "ro" and core["made"][0]["database_name"] is None


def test_a_named_database_rides_the_request(core):
    _submit(database_name="ledger")
    assert core["made"][0]["database_name"] == "ledger"


@pytest.mark.parametrize("setup,kw,field,status", [
    ({"kill": True}, {}, "reason", 503),
    ({}, {"window_minutes": 17}, "window", 400),
    ({}, {"reason": "why"}, "reason", 400),
    ({"target": False}, {}, "target", 403),
    ({"db": False}, {"database_name": "ledger"}, "database", 403),
    ({"pending": {"id": 3}}, {}, "target", 409),
    ({"gone": True}, {}, "target", 404),
    ({"created": None}, {}, "target", 409),
])
def test_each_refusal_names_its_field_and_status(core, setup, kw, field, status):
    core.update(setup)
    with pytest.raises(aar.WindowRequestRefused) as e:
        _submit(**kw)
    assert (e.value.field, e.value.status) == (field, status)


# --- the Slack modal ---------------------------------------------------------

def test_the_slack_modal_maps_a_refusal_onto_its_field(core, monkeypatch):
    from queryhub.slack_app import handlers, ro_window
    core["pending"] = {"id": 3}
    acks = []
    body = {"user": {"id": UID, "name": "Example"}, "view": {"state": {"values": {
        ro_window.B_TARGET: {ro_window.A_TARGET: {"selected_option": {"value": "53"}}},
        ro_window.B_WINDOW: {ro_window.A_WINDOW: {"selected_option": {"value": "60"}}},
        ro_window.B_REASON: {ro_window.A_REASON: {"value": "reading the ledger"}}}}}}
    monkeypatch.setattr(handlers, "_kill_switch_on", lambda: False)
    handlers.handle_ro_window_submission(lambda *a, **k: acks.append(a[0] if a else k), body, None)
    assert acks == [{"response_action": "errors",
                     "errors": {ro_window.B_TARGET: "You already have a pending window request for this target."}}]


# --- the web form ------------------------------------------------------------

@pytest.fixture
def web(core, monkeypatch):
    from queryhub.web import routes_requests as rr
    st = {"notified": [], "client": object()}
    monkeypatch.setattr(targets, "by_alias", lambda a: T if a == "prod-ledger" else None)
    monkeypatch.setattr(aar, "notify_admins",
                        lambda client, row, alias: st["notified"].append(alias) or 2)
    import queryhub.web.routes_queries as rq
    monkeypatch.setattr(rq, "_bot_client", lambda: st["client"])
    st["rr"] = rr
    return st


def test_the_web_files_it_and_sends_the_admins_the_card(web):
    rr = web["rr"]
    out = rr.create_window_request(rr.WindowRequestIn(connectionId="prod-ledger",
                                                      reason="reading the ledger"),
                                   claims={"sub": UID, "name": "Example"})
    assert out["id"] == 7 and out["tier"] == "RO" and out["adminsNotified"] == 2
    assert web["notified"] == ["prod-ledger"]


def test_the_web_answers_a_refusal_with_its_status_and_field(web, core):
    rr = web["rr"]
    core["target"] = False
    with pytest.raises(HTTPException) as e:
        rr.create_window_request(rr.WindowRequestIn(connectionId="prod-ledger",
                                                    reason="reading the ledger"),
                                 claims={"sub": UID})
    assert e.value.status_code == 403 and e.value.detail["field"] == "target"


def test_an_unknown_connection_is_a_404(web):
    rr = web["rr"]
    with pytest.raises(HTTPException) as e:
        rr.create_window_request(rr.WindowRequestIn(connectionId="nope", reason="x" * 10),
                                 claims={"sub": UID})
    assert e.value.status_code == 404


def test_without_slack_the_request_is_still_filed(web):
    rr = web["rr"]
    web["client"] = None
    out = rr.create_window_request(rr.WindowRequestIn(connectionId="prod-ledger",
                                                      reason="reading the ledger"),
                                   claims={"sub": UID})
    assert out["adminsNotified"] == 0 and web["notified"] == []


def test_the_lengths_offered_are_the_slack_modals_own(web, monkeypatch):
    from queryhub.slack_app import ro_window
    rr = web["rr"]
    monkeypatch.setattr(aar, "list_for", lambda pid, limit=50: [])
    out = rr.my_window_requests(claims={"sub": UID})
    mins = [o["minutes"] for o in out["windowOptions"]]
    assert mins[:3] == [m for m, _ in ro_window.WINDOW_OPTIONS]
    assert mins[3:] == [d * 1440 for d in aar.DAY_WINDOWS]


# --- tiers and day windows (design 2026-09-22 §3) ------------------------------

def test_a_day_window_is_accepted(core):
    _submit(window_minutes=7 * 1440)
    assert core["made"][0]["window_minutes"] == 7 * 1440


def test_writes_may_be_asked_for_by_someone_who_can_write(core):
    _submit(tier="RW", database_name="ledger")
    assert core["made"][0]["max_tier"] == "rw"


def test_writes_are_refused_to_someone_who_can_only_read(core):
    core["held"] = "ro"
    with pytest.raises(aar.WindowRequestRefused) as e:
        _submit(tier="rw", database_name="ledger")
    assert (e.value.field, e.value.status) == ("tier", 403)


def test_schema_changes_cannot_be_asked_for(core):
    with pytest.raises(aar.WindowRequestRefused) as e:
        _submit(tier="DDL")
    assert (e.value.field, e.value.status) == ("tier", 400)


def test_the_web_turns_days_into_the_window(web, core):
    rr = web["rr"]
    core["created"] = {**core["created"], "window_minutes": 14 * 1440, "max_tier": "rw"}
    out = rr.create_window_request(rr.WindowRequestIn(connectionId="prod-ledger", tier="RW",
                                                      days=14, reason="migration week",
                                                      databaseId="ledger"),
                                   claims={"sub": UID})
    assert core["made"][0]["window_minutes"] == 14 * 1440
    assert out["days"] == 14 and out["tier"] == "RW"


# --- one decision for Slack and the web ------------------------------------------

@pytest.fixture
def decision(monkeypatch):
    from queryhub import admins, db as dbm
    st = {"req": {"id": 7, "status": "pending", "requester_slack_id": "U0EXAMPLE002",
                  "target_server_id": 53, "database_name": None, "max_tier": "ro",
                  "window_minutes": 1440, "reason": "a report"},
          "admin": True, "scope": True, "sql": [], "rowcount": 1, "decided": {"requester_slack_id": "U0EXAMPLE002"}}
    monkeypatch.setattr(admins, "is_admin", lambda pid: st["admin"])
    monkeypatch.setattr(admins, "can_approve", lambda pid, req: st["scope"])
    monkeypatch.setattr(aar, "get", lambda rid: st["req"])
    monkeypatch.setattr(aar, "decide", lambda rid, **k: st["decided"])
    monkeypatch.setattr(targets, "get", lambda tid: T)
    monkeypatch.setattr(dbm, "execute", lambda sql, params=None: st["sql"].append(sql))

    class Cur:
        rowcount = 1
        def execute(self, sql, params=None):
            st["sql"].append(sql); Cur.rowcount = st["rowcount"]
        def fetchone(self): return {"id": 99}
        def __enter__(self): return self
        def __exit__(self, *e): return False

    class Conn:
        def cursor(self): return Cur()
        def commit(self): st["sql"].append("COMMIT")
        def rollback(self): st["sql"].append("ROLLBACK")
        def __enter__(self): return self
        def __exit__(self, *e): return False
    monkeypatch.setattr(dbm, "connection", lambda: Conn())
    return st


def test_approving_writes_the_waiver_starting_at_the_decision(decision):
    out = aar.decide_window(7, approve=True, actor_id="U0EXAMPLE001", actor_name="Admin")
    assert out["status"] == "approved" and out["grant_id"] == 99
    ins = next(q for q in decision["sql"] if "INSERT INTO auto_approve_grants" in q)
    assert "NOW() + make_interval(mins => %s)" in ins
    assert any("auto_approve_window_approved" in q for q in decision["sql"])


def test_a_scoped_admin_outside_scope_is_refused(decision):
    decision["scope"] = False
    with pytest.raises(aar.WindowDecisionRefused) as e:
        aar.decide_window(7, approve=True, actor_id="U0EXAMPLE001", actor_name="Admin")
    assert e.value.status == 403


def test_a_second_decision_finds_it_decided(decision):
    decision["rowcount"] = 0
    with pytest.raises(aar.WindowDecisionRefused) as e:
        aar.decide_window(7, approve=True, actor_id="U0EXAMPLE001", actor_name="Admin")
    assert e.value.status == 409 and "ROLLBACK" in decision["sql"]


def test_declining_is_recorded(decision):
    out = aar.decide_window(7, approve=False, actor_id="U0EXAMPLE001", actor_name="Admin")
    assert out["status"] == "rejected"
    assert any("auto_approve_window_rejected" in q for q in decision["sql"])


def test_the_slack_card_and_the_web_screen_share_the_decision():
    import inspect
    from queryhub.slack_app import handlers
    from queryhub.web import routes_admin as ra
    for fn in (handlers.handle_ro_window_approve, handlers.handle_ro_window_reject,
               ra.admin_decide_window_request):
        assert "decide_window(" in inspect.getsource(fn), fn.__name__


def test_a_day_window_reads_as_days():
    from queryhub.slack_app import ro_window
    assert ro_window.window_label(480) == "8h"
    assert ro_window.window_label(1440) == "1 day"
    assert ro_window.window_label(7 * 1440) == "7 days"
