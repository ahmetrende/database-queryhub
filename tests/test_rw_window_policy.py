"""Auto-approve windows that cover writes (RW): a switch, off by default.

The operator's decision (PLA-1592 item U3). A person can ask for a RW window
only while `auto_approve_rw_requests` is on (migration 150). While it is off,
only a read-only window is possible, and nobody can approve a pending RW
window. While it is on, these rules apply:

* Never for schema changes (DDL).
* Always a window with an end time.
* On a production connection, only a super-admin decides it.
* The waiver grants nothing. The person must hold RW there.

The web form and the Slack card share `auto_approve_requests`. So these tests
check each rule there once, and each surface for how it says the refusal.
"""
import contextlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import (admins, auto_approve, auto_approve_requests as aar,
                           core_submit, targets, teams)
from queryhub import config as cfg
from queryhub.web import mapping

REQUESTER = "U0EXAMPLE002"
SUPER = "U0EXAMPLE001"
ADMIN = "U0EXAMPLE003"           # an admin, not a super-admin
PROD = SimpleNamespace(id=53, alias="prod-main")
STAGE = SimpleNamespace(id=54, alias="stage-main")


@pytest.fixture
def switch(monkeypatch):
    """The value of `auto_approve_rw_requests`. Off, as the migration seeds it."""
    state = {"value": "off", "fail": False}
    stub = cfg.get_setting

    def get_setting(key, default=None):
        if key == "auto_approve_rw_requests":
            if state["fail"]:
                raise RuntimeError("control database unreachable")
            return state["value"]
        return stub(key, default)
    monkeypatch.setattr(cfg, "get_setting", get_setting)
    return state


# --- asking for a window -------------------------------------------------------

@pytest.fixture
def ask(monkeypatch, switch):
    st = {"held": "rw", "made": []}
    monkeypatch.setattr(core_submit, "kill_switch_on", lambda: False)
    monkeypatch.setattr(teams, "can_use_target", lambda pid, tid: True)
    monkeypatch.setattr(teams, "can_use_database", lambda pid, tid, d: True)
    monkeypatch.setattr(teams, "effective_mode_for_database", lambda pid, tid, d: st["held"])
    monkeypatch.setattr(teams, "effective_grant_for_user", lambda pid, tid: {"mode": st["held"]})
    monkeypatch.setattr(auto_approve, "validate_scope", lambda tid, d: None)
    monkeypatch.setattr(aar, "find_pending_for", lambda pid, tid: None)
    monkeypatch.setattr(targets, "get", lambda tid: PROD)

    def create(**kw):
        st["made"].append(kw)
        return {"id": 7, "status": "pending", "database_name": kw["database_name"],
                "window_minutes": kw["window_minutes"], "max_tier": kw["max_tier"],
                "requester_slack_id": REQUESTER}
    monkeypatch.setattr(aar, "create", create)
    st["switch"] = switch
    return st


def _ask(tier, **kw):
    args = dict(principal_id=REQUESTER, name="Jordan Ray", target_id=PROD.id,
                window_minutes=1440, reason="a week of backfill writes",
                database_name="ledger", tier=tier)
    args.update(kw)
    return aar.submit_window(**args)


def test_rw_is_refused_while_the_switch_is_off(ask):
    with pytest.raises(aar.WindowRequestRefused) as e:
        _ask("RW")
    assert (e.value.field, e.value.status) == ("tier", 403)
    assert e.value.message == aar.RW_OFF_MESSAGE
    assert ask["made"] == []


def test_reads_are_filed_while_the_switch_is_off(ask):
    _ask("RO")
    assert ask["made"][0]["max_tier"] == "ro"


def test_rw_is_filed_while_the_switch_is_on(ask):
    ask["switch"]["value"] = "on"
    _ask("rw")
    assert ask["made"][0]["max_tier"] == "rw"
    assert ask["made"][0]["window_minutes"] == 1440


def test_rw_still_needs_rw_access_while_the_switch_is_on(ask):
    ask["switch"]["value"] = "on"
    ask["held"] = "ro"
    with pytest.raises(aar.WindowRequestRefused) as e:
        _ask("RW")
    assert (e.value.field, e.value.status) == ("tier", 403)
    assert "read-only" in e.value.message and ask["made"] == []


@pytest.mark.parametrize("value", ["off", "on"])
def test_ddl_is_never_filed(ask, value):
    ask["switch"]["value"] = value
    with pytest.raises(aar.WindowRequestRefused) as e:
        _ask("DDL")
    assert (e.value.field, e.value.status) == ("tier", 400)
    assert ask["made"] == []


def test_a_switch_that_cannot_be_read_counts_as_off(ask):
    ask["switch"].update(value="on", fail=True)
    assert aar.rw_requests_enabled() is False
    with pytest.raises(aar.WindowRequestRefused) as e:
        _ask("RW")
    assert e.value.message == aar.RW_OFF_MESSAGE


@pytest.mark.parametrize("value,on", [
    ("on", True), ("ON", True), ("true", True), ("yes", True), ("1", True),
    ("off", False), ("", False), ("false", False), ("maybe", False)])
def test_the_switch_vocabulary(switch, value, on):
    switch["value"] = value
    assert aar.rw_requests_enabled() is on
    assert aar.offered_tiers() == (["RO", "RW"] if on else ["RO"])


# --- the web form ----------------------------------------------------------------

@pytest.fixture
def web(ask, monkeypatch):
    from queryhub.web import routes_requests as rr
    import queryhub.web.routes_queries as rq
    monkeypatch.setattr(targets, "by_alias", lambda a: PROD if a == PROD.alias else None)
    monkeypatch.setattr(aar, "notify_admins", lambda client, row, alias: 1)
    monkeypatch.setattr(aar, "list_for", lambda pid, limit=50: [])
    monkeypatch.setattr(rq, "_bot_client", lambda: None)
    return rr


def test_the_web_says_which_tiers_are_on_offer(web, ask):
    claims = {"sub": REQUESTER}
    assert web.my_window_requests(claims=claims)["tiers"] == ["RO"]
    ask["switch"]["value"] = "on"
    assert web.my_window_requests(claims=claims)["tiers"] == ["RO", "RW"]


def test_the_web_refuses_rw_with_403_on_the_tier_field(web, ask):
    body = web.WindowRequestIn(connectionId=PROD.alias, databaseId="ledger", tier="RW",
                               days=7, reason="a week of backfill writes")
    with pytest.raises(HTTPException) as e:
        web.create_window_request(body, claims={"sub": REQUESTER})
    assert e.value.status_code == 403
    assert e.value.detail == {"code": "window_refused", "message": aar.RW_OFF_MESSAGE,
                              "field": "tier"}
    ask["switch"]["value"] = "on"
    out = web.create_window_request(body, claims={"sub": REQUESTER})
    assert out["tier"] == "RW" and out["days"] == 7


# --- which connection is production ---------------------------------------------

@pytest.mark.parametrize("alias,env", [
    ("prod-main", "production"), ("orders", "production"), ("", "production"),
    (None, "production"), ("stage-main", "staging"), ("orders-staging", "staging"),
    ("orders-test", "staging"), ("Orders-TEST", "staging")])
def test_one_rule_says_production_for_the_rule_and_for_the_env_dot(alias, env):
    assert targets.environment(alias) == env
    assert mapping.env_of(alias) == env
    assert aar.on_production(alias) is (env == "production")


# --- deciding a window -------------------------------------------------------------

@pytest.fixture
def decide(monkeypatch, switch):
    from queryhub import db as dbm
    st = {"req": {"id": 7, "status": "pending", "requester_slack_id": REQUESTER,
                  "target_server_id": PROD.id, "database_name": "ledger",
                  "max_tier": "rw", "window_minutes": 1440, "reason": "a backfill"},
          "target": PROD, "super": {SUPER}, "scope": True, "held": "rw",
          "sql": [], "audit": [], "inserted": [], "returned": {"id": 99}}
    monkeypatch.setattr(admins, "is_admin", lambda pid: pid in (SUPER, ADMIN))
    monkeypatch.setattr(admins, "is_super_admin", lambda pid: pid in st["super"])
    monkeypatch.setattr(admins, "can_approve", lambda pid, req: st["scope"])
    monkeypatch.setattr(aar, "get", lambda rid: dict(st["req"]))
    monkeypatch.setattr(targets, "get", lambda tid: st["target"])
    monkeypatch.setattr(teams, "effective_mode_for_database", lambda pid, tid, d: st["held"])
    monkeypatch.setattr(teams, "effective_grant_for_user", lambda pid, tid: {"mode": st["held"]})

    class TxCur:
        def execute(self, sql, params=None):
            st["sql"].append(sql)
            if "INSERT INTO audit_log" in sql:
                st["audit"].append(params)

        def fetchone(self):
            return {"requester_slack_id": REQUESTER}

    @contextlib.contextmanager
    def transaction():
        yield TxCur()
    monkeypatch.setattr(dbm, "transaction", transaction)

    class Cur:
        rowcount = 1

        def execute(self, sql, params=None):
            st["sql"].append(sql)
            if "INSERT INTO auto_approve_grants" in sql:
                st["inserted"].append(params)
            if "INSERT INTO audit_log" in sql:
                st["audit"].append(params)

        def fetchone(self):
            return st["returned"]

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            st["sql"].append("COMMIT")

        def rollback(self):
            st["sql"].append("ROLLBACK")

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False
    monkeypatch.setattr(dbm, "connection", lambda: Conn())
    st["switch"] = switch
    switch["value"] = "on"
    return st


def _decide(actor, approve=True):
    return aar.decide_window(7, approve=approve, actor_id=actor, actor_name="Jordan Ray")


@pytest.mark.parametrize("tier", ["rw", "ddl"])
@pytest.mark.parametrize("approve", [True, False])
def test_rw_on_production_is_a_super_admins_decision(decide, approve, tier):
    decide["req"]["max_tier"] = tier
    with pytest.raises(aar.WindowDecisionRefused) as e:
        _decide(ADMIN, approve=approve)
    assert (e.value.status, e.value.code) == (403, "super_admin")
    assert e.value.message == aar.PRODUCTION_WRITE_MESSAGE
    assert decide["sql"] == [] and decide["audit"] == []


def test_a_super_admin_approves_rw_on_production_with_an_end(decide):
    from datetime import datetime, timezone
    end = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    decide["returned"] = {"id": 99, "expires_at": end}
    out = _decide(SUPER)
    assert out["status"] == "approved" and out["grant_id"] == 99
    (params,) = decide["inserted"]
    assert params[1] == "rw" and params[4] == 1440       # tier, window minutes
    ins = next(q for q in decide["sql"] if "INSERT INTO auto_approve_grants" in q)
    assert "NOW() + make_interval(mins => %s)" in ins and "RETURNING id, expires_at" in ins
    details = json.loads(decide["audit"][0][2])
    assert details["max_tier"] == "rw" and details["window_minutes"] == 1440
    assert details["expires_at"] == end.isoformat()
    assert "COMMIT" in decide["sql"]


def test_an_admin_in_scope_decides_rw_on_staging(decide):
    decide["target"] = STAGE
    assert _decide(ADMIN)["status"] == "approved"
    assert _decide(ADMIN, approve=False)["status"] == "rejected"


def test_a_read_window_on_production_stays_with_every_admin_in_scope(decide):
    decide["req"]["max_tier"] = "ro"
    assert _decide(ADMIN)["status"] == "approved"


def test_a_deleted_connection_counts_as_production(decide):
    decide["target"] = None
    with pytest.raises(aar.WindowDecisionRefused) as e:
        _decide(ADMIN)
    assert e.value.code == "super_admin"


def test_rw_is_not_approved_while_the_switch_is_off_but_can_be_rejected(decide):
    decide["switch"]["value"] = "off"
    with pytest.raises(aar.WindowDecisionRefused) as e:
        _decide(SUPER)
    assert (e.value.status, e.value.code) == (409, "rw_off")
    assert decide["inserted"] == [] and decide["audit"] == []
    assert _decide(SUPER, approve=False)["status"] == "rejected"


def test_rw_is_not_approved_when_the_requester_lost_rw(decide):
    decide["held"] = "ro"
    with pytest.raises(aar.WindowDecisionRefused) as e:
        _decide(SUPER)
    assert (e.value.status, e.value.code) == (409, "rw_not_held")
    assert decide["inserted"] == []


def test_the_rw_check_at_the_decision_asks_per_database(decide, monkeypatch):
    """RW on another database of the connection is not RW on this one."""
    monkeypatch.setattr(teams, "effective_mode_for_database",
                        lambda pid, tid, d: "rw" if d == "other" else "ro")
    with pytest.raises(aar.WindowDecisionRefused) as e:
        _decide(SUPER)
    assert e.value.code == "rw_not_held"


def test_a_ddl_window_is_never_approved(decide):
    decide["req"]["max_tier"] = "ddl"
    with pytest.raises(aar.WindowDecisionRefused) as e:
        _decide(SUPER)
    assert (e.value.status, e.value.code) == (409, "ddl")
    assert decide["inserted"] == []


@pytest.mark.parametrize("minutes", [None, 0, -60])
def test_a_window_with_no_end_is_never_approved(decide, minutes):
    decide["req"]["window_minutes"] = minutes
    with pytest.raises(aar.WindowDecisionRefused) as e:
        _decide(SUPER)
    assert e.value.code == "no_window" and decide["inserted"] == []


# --- the admin screen lists what this admin may decide -----------------------------

def test_the_admin_list_hides_rw_on_production_from_an_admin_who_is_not_super(
        decide, monkeypatch):
    from queryhub.web import admin as web_admin, routes_admin as ra
    rows = [dict(decide["req"], id=1, alias=PROD.alias, max_tier="rw",
                 requester_name="Jordan Ray", created_at=None),
            dict(decide["req"], id=2, alias=STAGE.alias, max_tier="rw",
                 requester_name="Jordan Ray", created_at=None),
            dict(decide["req"], id=3, alias=PROD.alias, max_tier="ro",
                 requester_name="Jordan Ray", created_at=None)]
    monkeypatch.setattr(aar, "list_pending", lambda limit=200: rows)
    monkeypatch.setattr(web_admin, "require_admin", lambda claims, need="review", **k: claims["sub"])
    ids = lambda who: [r["id"] for r in ra.admin_window_requests(claims={"sub": who})["requests"]]  # noqa: E731
    assert ids(ADMIN) == [2, 3]
    assert ids(SUPER) == [1, 2, 3]


# --- the Slack card says the rule ------------------------------------------------------

@pytest.fixture
def slack(decide, monkeypatch):
    from queryhub.slack_app import handlers
    st = {"dms": [], "card": []}
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda client, uid, text, **k: st["dms"].append((uid, text)))
    monkeypatch.setattr(handlers, "_ro_window_update_admin_msg",
                        lambda client, body, text: st["card"].append(text))
    st["h"] = handlers
    return st


def _click(actor):
    return {"user": {"id": actor, "name": "Jordan Ray"}, "actions": [{"value": "7"}],
            "container": {"channel_id": "D1", "message_ts": "1.0"}}


@pytest.mark.parametrize("handler", ["handle_ro_window_approve", "handle_ro_window_reject"])
def test_the_slack_card_tells_the_admin_which_rule_refused(slack, handler):
    getattr(slack["h"], handler)(lambda *a, **k: None, _click(ADMIN), object())
    assert slack["dms"] == [(ADMIN, f":no_entry: Window request `#7`: {aar.PRODUCTION_WRITE_MESSAGE}")]
    assert slack["card"] == []           # still pending: the card keeps its buttons


def test_the_slack_card_says_the_switch_is_off(slack, decide):
    decide["switch"]["value"] = "off"
    slack["h"].handle_ro_window_approve(lambda *a, **k: None, _click(SUPER), object())
    assert slack["dms"] == [(SUPER, f":no_entry: Window request `#7`: {aar.RW_OFF_DECISION_MESSAGE}")]
    assert slack["card"] == []
