"""The kill switch stops approvals too (operator decision, 2026-10-24).

While the switch is on, every action that approves and runs a request is
refused with "Kill switch is on. Resume queries first.":

  - the web decision with approve, the web batch approve, the web batch decision
  - the Slack Approve button and "Approve all remaining"
  - the Slack approval of a CSV import

Deny and Request changes stay open. With the switch off, every path works as
before. The tests use fakes: no database, no Slack.
"""
from unittest.mock import MagicMock

import pytest

from queryhub import core_decide, core_submit
from queryhub.slack_app import handlers
from queryhub.web import routes_admin as ra

SAY = "Kill switch is on. Resume queries first."
DBA = "U0EXAMPLE002"
CLAIMS = {"sub": DBA, "name": "Sam Archer"}


@pytest.fixture
def kill(monkeypatch):
    """`kill["on"] = True` turns the switch on."""
    state = {"on": False}
    monkeypatch.setattr(core_submit, "kill_switch_on", lambda: state["on"])
    return state


class _Cur:
    def __init__(self, items=()):
        self.items = list(items)
        self.executed = []
        self._one = None

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        if flat.startswith("UPDATE requests"):
            self._one = {"id": params[-1], "status": params[0], "bundle_id": 311,
                         "requester_slack_id": "U0EXAMPLE001",
                         "decided_by_slack_id": DBA,
                         "decision_reason": params[1] if params[0] != "approved" else None}

    def fetchall(self):
        return list(self.items)

    def fetchone(self):
        return self._one


class _Txn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


def _item(i):
    return {"id": i, "status": "pending", "scheduled_for": None,
            "requester_slack_id": "U0EXAMPLE001", "decided_by_slack_id": None,
            "decided_by_name": None, "query": "SELECT 1", "target_server_id": 3,
            "engine": "postgres", "required_tier": "ro"}


@pytest.fixture
def db(monkeypatch):
    """A fake transaction, and a fake `requests` read for decide()."""
    state = {"cur": _Cur([_item(2045), _item(2046)]), "audit": []}
    monkeypatch.setattr(core_decide.db, "transaction", lambda: _Txn(state["cur"]))
    monkeypatch.setattr(core_decide.db, "fetch_one",
                        lambda *a, **k: {"scheduled_for": None})
    monkeypatch.setattr(core_decide.audit, "log_in",
                        lambda c, rid, bid, bname, action, details=None:
                        state["audit"].append((rid, action)))
    return state


def _updates(cur):
    return [s for s, _ in cur.executed if s.startswith("UPDATE requests")]


# ---- core_decide ----------------------------------------------------------------

def test_the_refusal_says_the_agreed_words(kill):
    kill["on"] = True
    with pytest.raises(core_decide.KillSwitchOn) as e:
        core_decide.require_kill_switch_off()
    assert e.value.message == SAY
    assert e.value.code == "kill_switch_on"


def test_off_does_not_raise(kill):
    core_decide.require_kill_switch_off()


def test_decide_approve_is_refused_before_anything_is_written(kill, db):
    kill["on"] = True
    with pytest.raises(core_decide.KillSwitchOn):
        core_decide.decide(7, "approve", by_id=DBA, by_name="sam")
    assert db["cur"].executed == []
    assert db["audit"] == []


@pytest.mark.parametrize("decision", ["reject", "changes"])
def test_decide_deny_and_changes_still_work_with_the_switch_on(kill, db, decision):
    kill["on"] = True
    out = core_decide.decide(7, decision, by_id=DBA, by_name="sam",
                             reason="needs a WHERE clause")
    assert out is not None and out.decision == decision


def test_decide_approve_works_with_the_switch_off(kill, db):
    out = core_decide.decide(7, "approve", by_id=DBA, by_name="sam")
    assert out is not None and out.decision == "approve"


def test_decide_bundle_approve_is_refused_and_writes_nothing(kill, db):
    kill["on"] = True
    with pytest.raises(core_decide.KillSwitchOn):
        core_decide.decide_bundle(311, "approve", by_id=DBA, by_name="sam",
                                  may_decide=lambda r: True)
    assert db["cur"].executed == []


def test_decide_bundle_reject_works_with_the_switch_on(kill, db):
    kill["on"] = True
    out = core_decide.decide_bundle(311, "reject", by_id=DBA, by_name="sam",
                                    reason="wrong database for all of them",
                                    may_decide=lambda r: True)
    assert [o.row["id"] for o in out] == [2045, 2046]


def test_decide_bundle_approve_works_with_the_switch_off(kill, db):
    out = core_decide.decide_bundle(311, "approve", by_id=DBA, by_name="sam",
                                    may_decide=lambda r: True)
    assert len(out) == 2


# ---- the web routes -------------------------------------------------------------

@pytest.fixture
def web(monkeypatch, db):
    seen = {"effects": []}
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need="review", **k: DBA)
    monkeypatch.setattr(ra, "_slack_client", lambda: None)
    monkeypatch.setattr(ra.db, "fetch_one", lambda *a, **k: {"id": 7})
    monkeypatch.setattr(ra.admins, "can_approve", lambda uid, row: True)
    monkeypatch.setattr(ra.core_decide, "apply_effects",
                        lambda client, o: seen["effects"].append((o.row["id"], o.decision)))
    return seen


def _refusal(exc):
    assert exc.status_code == 409
    assert exc.detail["code"] == "kill_switch_on"
    assert exc.detail["message"] == SAY


def test_web_decision_approve_is_409_with_the_switch_on(kill, web):
    kill["on"] = True
    with pytest.raises(Exception) as e:
        ra.admin_decision(7, ra.DecisionIn(decision="approve"), claims=CLAIMS)
    _refusal(e.value)
    assert web["effects"] == []


@pytest.mark.parametrize("decision", ["reject", "changes"])
def test_web_decision_deny_and_changes_work_with_the_switch_on(kill, web, decision):
    kill["on"] = True
    out = ra.admin_decision(7, ra.DecisionIn(decision=decision, note="needs a WHERE clause"),
                            claims=CLAIMS)
    assert out["id"] == "7"
    assert web["effects"] == [(7, decision)]


def test_web_decision_approve_works_with_the_switch_off(kill, web):
    out = ra.admin_decision(7, ra.DecisionIn(decision="approve"), claims=CLAIMS)
    assert out["status"]
    assert web["effects"] == [(7, "approve")]


def test_web_batch_approve_is_409_and_approves_nothing_with_the_switch_on(kill, web):
    kill["on"] = True
    with pytest.raises(Exception) as e:
        ra.admin_batch_approve(ra.BatchIn(ids=["7", "8"]), claims=CLAIMS)
    _refusal(e.value)
    assert web["effects"] == []


def test_web_batch_approve_works_with_the_switch_off(kill, web):
    out = ra.admin_batch_approve(ra.BatchIn(ids=["7", "8"]), claims=CLAIMS)
    assert out == {"approved": 2}


def test_web_batch_approve_stops_when_the_switch_goes_on_mid_loop(kill, web, monkeypatch):
    """The first item runs. The switch goes on. The second is refused."""
    real = ra.core_decide.decide

    def decide(rid, decision, **kw):
        out = real(rid, decision, **kw)
        kill["on"] = True
        return out
    monkeypatch.setattr(ra.core_decide, "decide", decide)
    out = ra.admin_batch_approve(ra.BatchIn(ids=["7", "8"]), claims=CLAIMS)
    assert out == {"approved": 1}


def test_web_bundle_approve_is_409_with_the_switch_on(kill, web):
    kill["on"] = True
    with pytest.raises(Exception) as e:
        ra.admin_bundle_decision(311, ra.DecisionIn(decision="approve"), claims=CLAIMS)
    _refusal(e.value)
    assert web["effects"] == []


def test_web_bundle_reject_works_with_the_switch_on(kill, web):
    kill["on"] = True
    out = ra.admin_bundle_decision(
        311, ra.DecisionIn(decision="reject", note="wrong database for all of them"),
        claims=CLAIMS)
    assert [i["id"] for i in out["items"]] == ["2045", "2046"]


def test_web_bundle_approve_works_with_the_switch_off(kill, web):
    out = ra.admin_bundle_decision(311, ra.DecisionIn(decision="approve"), claims=CLAIMS)
    assert len(out["items"]) == 2


# ---- the Slack handlers ---------------------------------------------------------

BODY = {"user": {"id": DBA, "name": "sam"},
        "channel": {"id": "D0EXAMPLE01"},
        "actions": [{"value": "7"}]}


@pytest.fixture
def slack(monkeypatch, db):
    seen = {"effects": [], "submitted": [], "dms": [], "imports": []}
    monkeypatch.setattr(handlers, "_guard_admin", lambda *a, **k: True)
    monkeypatch.setattr(handlers, "_guard_bundle", lambda *a, **k: True)
    monkeypatch.setattr(handlers.core_decide, "apply_effects",
                        lambda client, o: seen["effects"].append(o.row["id"]))
    monkeypatch.setattr(handlers.executor, "submit",
                        lambda row, client: seen["submitted"].append(row["id"]))
    monkeypatch.setattr(handlers.executor, "submit_import",
                        lambda imp, client: seen["imports"].append(imp["id"]))
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda client, uid, text=None, **k: seen["dms"].append(text))
    monkeypatch.setattr(handlers.notifications, "update_bundle_admin_dms",
                        lambda *a, **k: None)
    return seen


def test_slack_approve_is_refused_with_an_ephemeral_message(kill, slack):
    kill["on"] = True
    client = MagicMock()
    handlers.handle_approve(MagicMock(), BODY, client)
    client.chat_postEphemeral.assert_called_once_with(
        channel="D0EXAMPLE01", user=DBA, text=SAY)
    assert slack["effects"] == [] and slack["submitted"] == []


def test_slack_approve_falls_back_to_a_dm_without_a_channel(kill, slack):
    kill["on"] = True
    body = {k: v for k, v in BODY.items() if k != "channel"}
    handlers.handle_approve(MagicMock(), body, MagicMock())
    assert slack["dms"] == [SAY]


def test_slack_approve_falls_back_to_a_dm_when_the_ephemeral_call_fails(kill, slack):
    kill["on"] = True
    client = MagicMock()
    client.chat_postEphemeral.side_effect = RuntimeError("channel_not_found")
    handlers.handle_approve(MagicMock(), BODY, client)
    assert slack["dms"] == [SAY]


def test_slack_approve_works_with_the_switch_off(kill, slack):
    handlers.handle_approve(MagicMock(), BODY, MagicMock())
    assert slack["effects"] == [7]


def test_slack_deny_still_opens_the_reason_dialog_with_the_switch_on(kill, slack):
    kill["on"] = True
    client = MagicMock()
    handlers.handle_reject(MagicMock(), {**BODY, "trigger_id": "T0EXAMPLE"}, client)
    client.views_open.assert_called_once()


def test_slack_approve_all_remaining_is_refused_and_writes_nothing(kill, slack, db, monkeypatch):
    kill["on"] = True
    monkeypatch.setattr(handlers, "_bundle_pending_items_in_scope",
                        lambda bid, uid: [_item(2045), _item(2046)])
    body = {**BODY, "actions": [{"value": "311"}]}
    client = MagicMock()
    handlers.handle_bundle_approve_all(MagicMock(), body, client)
    client.chat_postEphemeral.assert_called_once_with(
        channel="D0EXAMPLE01", user=DBA, text=SAY)
    assert db["cur"].executed == []
    assert slack["submitted"] == []


def test_slack_approve_all_remaining_works_with_the_switch_off(kill, slack, db, monkeypatch):
    monkeypatch.setattr(handlers, "_bundle_pending_items_in_scope",
                        lambda bid, uid: [_item(2045), _item(2046)])
    body = {**BODY, "actions": [{"value": "311"}]}
    handlers.handle_bundle_approve_all(MagicMock(), body, MagicMock())
    assert len(_updates(db["cur"])) == 2
    assert slack["submitted"] == [2045, 2046]


def test_slack_import_approval_is_refused_and_runs_nothing(kill, slack, monkeypatch):
    kill["on"] = True
    monkeypatch.setattr(handlers.admins, "is_admin", lambda uid: True)
    reads = []
    monkeypatch.setattr(handlers.db, "fetch_one", lambda *a, **k: reads.append(a))
    client = MagicMock()
    handlers.handle_import_approve(MagicMock(), BODY, client)
    client.chat_postEphemeral.assert_called_once_with(
        channel="D0EXAMPLE01", user=DBA, text=SAY)
    assert reads == [] and slack["imports"] == []


def test_slack_import_approval_continues_with_the_switch_off(kill, slack, monkeypatch):
    monkeypatch.setattr(handlers.admins, "is_admin", lambda uid: True)
    reads = []
    monkeypatch.setattr(handlers.db, "fetch_one", lambda *a, **k: reads.append(a))
    monkeypatch.setattr(handlers.notifications, "update_import_admin_messages",
                        lambda *a, **k: None)
    handlers.handle_import_approve(MagicMock(), BODY, MagicMock())
    assert len(reads) == 1      # it went on to read the pending import
