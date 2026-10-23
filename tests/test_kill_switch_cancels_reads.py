"""Setting the kill switch on cancels the running reads, and only the reads.

The operator's decision (PLA-1592 item U4c). When somebody sets the switch on,
QueryHub cancels every running read-only request of every user, through
`cancellation.cancel`. Writes and DDL keep running, because a stop in the
middle can leave them half applied. Setting the switch off cancels nothing.

The order matters. The switch and its `kill_switch_set` row commit first, in
one transaction, so no new query starts while the cancels run. Then the reads
stop, and a `kill_switch_cancelled` row records how many. The response carries
the same `cancelledCount`. A cancel that fails stops neither the switch nor the
other cancels.

tests/test_integration_policy_kill.py checks the SQL filter (status executing,
tier ro) on a real server. It runs a read and a write, and only the read stops.
"""
import contextlib
import json
import threading

import pytest

from queryhub import admins, cancellation, core_submit, kill_switch
from queryhub import db as dbm
from queryhub.cancellation import CancelOutcome
from queryhub.web import admin as web_admin, routes_admin as ra

SUPER = "U0EXAMPLE001"


# --- the sweep ---------------------------------------------------------------------

@pytest.fixture
def sweep(monkeypatch):
    st = {"running": [11, 12, 13], "outcomes": {}, "raise": set(), "calls": [],
          "list_fails": False}
    lock = threading.Lock()

    def fetch_all(sql, params=None):
        if st["list_fails"]:
            raise RuntimeError("control database unreachable")
        return [{"id": i} for i in st["running"]]
    monkeypatch.setattr(dbm, "fetch_all", fetch_all)

    def cancel(request_id, by_id, by_name):
        with lock:
            st["calls"].append((request_id, by_id, by_name))
        if request_id in st["raise"]:
            raise RuntimeError("target unreachable")
        return st["outcomes"].get(request_id, CancelOutcome.CANCELLED)
    monkeypatch.setattr(cancellation, "cancel", cancel)
    return st


def test_every_running_read_goes_through_cancel(sweep):
    sweep["outcomes"] = {12: CancelOutcome.TERMINATED, 13: CancelOutcome.NOT_RUNNING}
    assert cancellation.cancel_running_reads(SUPER, "Jordan Ray") == 2
    assert sorted(sweep["calls"]) == [(i, SUPER, "Jordan Ray") for i in (11, 12, 13)]


def test_a_cancel_that_fails_does_not_stop_the_others(sweep):
    sweep["raise"] = {11}
    sweep["outcomes"] = {12: CancelOutcome.FAILED}
    assert cancellation.cancel_running_reads(SUPER, "Jordan Ray") == 1
    assert sorted(c[0] for c in sweep["calls"]) == [11, 12, 13]


def test_nothing_running_cancels_nothing(sweep):
    sweep["running"] = []
    assert cancellation.cancel_running_reads(SUPER, "Jordan Ray") == 0
    assert sweep["calls"] == []


def test_a_list_that_cannot_be_read_is_an_unknown_count_not_zero(sweep):
    sweep["list_fails"] = True
    assert cancellation.cancel_running_reads(SUPER, "Jordan Ray") is None
    assert sweep["calls"] == []


# --- the two doors -----------------------------------------------------------------

@pytest.fixture
def world(monkeypatch, sweep):
    """A database that records each transaction, and each row it commits."""
    st = {"events": [], "rows": [], "config": [], "audit_fails": False}

    class Cur:
        def __init__(self):
            self.rows = []

        def execute(self, sql, params=None):
            if "INSERT INTO audit_log" in sql:
                if st["audit_fails"] and params[3] == kill_switch.CANCELLED_ACTION:
                    raise RuntimeError("audit insert failed")
                self.rows.append((params[3], json.loads(params[4]) if params[4] else None))
                st["events"].append(f"audit {params[3]}")
            elif "bot_config" in sql:
                st["config"].append(params)
                st["events"].append("config")

    @contextlib.contextmanager
    def transaction():
        st["events"].append("begin")
        cur = Cur()
        yield cur
        st["rows"].extend(cur.rows)            # visible only once committed
        st["events"].append("commit")
    monkeypatch.setattr(dbm, "transaction", transaction)
    monkeypatch.setattr(web_admin, "require_admin",
                        lambda claims, need="review", **k: claims["sub"])
    monkeypatch.setattr(core_submit, "kill_switch_message", lambda: "Paused for a migration.")
    real_cancel = cancellation.cancel

    def cancel(request_id, by_id, by_name):
        st["events"].append(f"cancel {request_id}")
        return real_cancel(request_id, by_id, by_name)
    monkeypatch.setattr(cancellation, "cancel", cancel)
    st["sweep"] = sweep
    return st


@pytest.fixture
def slack(monkeypatch, world):
    from queryhub.slack_app import subcommands
    said = []
    monkeypatch.setattr(admins, "is_super_admin", lambda pid: pid == SUPER)

    def fetch_one(sql, params=None):
        return {"value": "off"} if "kill_switch" in sql else {"name": "Jordan Ray"}
    monkeypatch.setattr(dbm, "fetch_one", fetch_one)
    monkeypatch.setattr(subcommands, "_respond", lambda respond, text: said.append(text))
    world["said"] = said
    world["run"] = lambda arg: subcommands._handle_kill(SUPER, arg, None, None, {})
    return world


def _web(enabled, message=None):
    return ra.set_kill(ra.KillIn(enabled=enabled, message=message),
                       claims={"sub": SUPER, "name": "Jordan Ray"})


SET_ON_TXN = ["begin", "config", "config", "audit kill_switch_set", "commit"]
CANCELLED_TXN = ["begin", "audit kill_switch_cancelled", "commit"]


def test_on_sets_the_switch_first_then_cancels_and_records_the_count(world):
    world["sweep"]["outcomes"] = {13: CancelOutcome.NOT_RUNNING}
    out = _web(True, "Deploying the ledger fix.")
    assert out == {"enabled": True, "message": "Paused for a migration.",
                   "cancelledCount": 2}
    assert world["rows"] == [
        ("kill_switch_set", {"enabled": True, "message": "Deploying the ledger fix."}),
        ("kill_switch_cancelled", {"cancelledCount": 2})]
    ev = world["events"]
    assert ev[:5] == SET_ON_TXN
    # The cancels run side by side, so their own order is not fixed.
    assert sorted(ev[5:8]) == ["cancel 11", "cancel 12", "cancel 13"]
    assert ev[8:] == CANCELLED_TXN


def test_on_without_a_message_records_the_message_in_force(world):
    _web(True)
    assert world["rows"][0] == ("kill_switch_set",
                                {"enabled": True, "message": "Paused for a migration."})


def test_off_cancels_nothing(world, monkeypatch):
    called = []
    monkeypatch.setattr(kill_switch, "stop_running_reads", lambda *a: called.append(a) or 5)
    out = _web(False)
    assert called == [] and world["sweep"]["calls"] == []
    assert out["cancelledCount"] == 0 and out["enabled"] is False
    assert world["rows"] == [("kill_switch_set", {"enabled": False})]
    assert world["config"] == [("off",)]


def test_a_failed_cancel_stops_neither_the_switch_nor_the_others(world):
    world["sweep"]["raise"] = {12}
    out = _web(True, "Deploying the ledger fix.")
    assert out["cancelledCount"] == 2
    assert world["config"][0] == ("on",) and world["events"][:5] == SET_ON_TXN
    assert sorted(c[0] for c in world["sweep"]["calls"]) == [11, 12, 13]


def test_a_sweep_that_cannot_read_records_an_unknown_count(world):
    world["sweep"]["list_fails"] = True
    out = _web(True)
    assert out["cancelledCount"] is None
    assert world["rows"][1] == ("kill_switch_cancelled", {"cancelledCount": None})


def test_a_failed_count_row_still_answers_with_the_count(world):
    world["audit_fails"] = True
    assert _web(True)["cancelledCount"] == 3
    assert [r[0] for r in world["rows"]] == ["kill_switch_set"]


@pytest.mark.parametrize("door", ["web", "slack"])
@pytest.mark.parametrize("step", ["slow", "raises"])
def test_the_switch_and_its_row_commit_before_the_cancels_start(slack, monkeypatch,
                                                                door, step):
    """When the cancel step is slow or raises, the switch and its audit row
    are already committed. During a hung cancel, no new query starts."""
    seen = {}

    def sweep(by_id, by_name):
        seen["committed"] = list(slack["rows"])
        seen["config"] = list(slack["config"])
        if step == "raises":
            raise RuntimeError("hung target")
        return 0
    monkeypatch.setattr(cancellation, "cancel_running_reads", sweep)
    if door == "web":
        assert _web(True)["cancelledCount"] == (0 if step == "slow" else None)
    else:
        slack["run"]("on")
    assert seen["config"] == [("on",)]
    assert [r[0] for r in seen["committed"]] == ["kill_switch_set"]
    assert slack["rows"][-1] == ("kill_switch_cancelled",
                                 {"cancelledCount": 0 if step == "slow" else None})


def test_the_route_keeps_its_authority():
    """U4c changes what the switch does, not who may set it. The level is the
    DBA level of U4a (tests/test_dba_level.py owns that rule)."""
    import inspect
    assert 'require_admin(claims, "dba")' in inspect.getsource(ra.set_kill)


# --- /sql kill ----------------------------------------------------------------------

def test_slack_on_sets_the_switch_then_cancels_the_reads_too(slack):
    slack["run"]("on")
    assert slack["rows"] == [
        ("kill_switch_set", {"enabled": True, "from": "off", "to": "on", "via": "slack",
                             "message": "Paused for a migration."}),
        ("kill_switch_cancelled", {"cancelledCount": 3})]
    assert "It cancelled 3 running read-only queries." in slack["said"][0]
    assert "Writes and DDL continue to run." in slack["said"][0]


def test_slack_off_cancels_nothing(slack):
    slack["run"]("off")
    assert slack["sweep"]["calls"] == []
    assert slack["rows"] == [("kill_switch_set", {"enabled": False, "from": "off",
                                                  "to": "off", "via": "slack"})]
    assert "cancelled" not in slack["said"][0]


def test_slack_says_when_the_count_is_not_known(slack):
    slack["sweep"]["list_fails"] = True
    slack["run"]("on")
    assert "could not read the running queries" in slack["said"][0]
