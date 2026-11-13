"""A Stop must read as a Stop, not as a statement timeout (no live DB).

pg_cancel_backend makes the driver raise QueryCanceled, and the executor thread
sees it before `cancellation.cancel` polls again. The executor used to word it
as a statement timeout, and `cancellation._fail_request` then found the row no
longer executing and wrote no `cancelled_running` row. On the live fleet that
was 13 of 13 Postgres stops between 2026-07 and 2026-11. A page that shows a
stopped query reads that audit row.
"""
from __future__ import annotations

import contextlib

import pytest

from queryhub import cancellation, executor

def test_the_runaway_watchdog_keeps_its_own_action():
    action, note = cancellation.stop_reason(*cancellation.RUNAWAY_ACTOR)
    assert action == "execution_runaway_stopped"
    assert note.startswith("Stopped after running longer than")
    assert cancellation.stop_reason(None, None) == (
        "cancelled_running", "Cancelled by a user while it was running.")


# ---- _fail writes the stop's audit row once --------------------------------

class _Cur:
    def __init__(self, st):
        self.st = st
        self.rowcount = 1
        self._one = None

    def execute(self, sql, params=None):
        if sql.startswith("SELECT 1 FROM audit_log"):
            self._one = (1,) if params[1] in self.st["present"] else None
        else:
            self.rowcount = 1

    def fetchone(self):
        return self._one


@pytest.fixture
def fail_env(monkeypatch):
    st = {"present": set(), "logged": []}

    @contextlib.contextmanager
    def txn():
        yield _Cur(st)

    monkeypatch.setattr(executor.db, "transaction", txn)
    monkeypatch.setattr(executor.audit, "log_in",
                        lambda cur, rid, actor, name, action, details=None:
                        st["logged"].append((action, actor, name, details)))
    monkeypatch.setattr(executor.notifications, "update_all_admin_messages", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_deliver_result_to_requester", lambda r: False)
    return st


def test_fail_writes_the_stop_action_with_the_person(fail_env):
    executor._fail(None, {"id": 5}, "Cancelled by Jordan Ray while it was running.",
                   stopped=("cancelled_running", "U0EXAMPLE001", "Jordan Ray"))
    assert fail_env["logged"] == [("cancelled_running", "U0EXAMPLE001", "Jordan Ray",
                                   {"note": "Cancelled by Jordan Ray while it was running."})]


def test_fail_does_not_repeat_a_stop_row_that_cancellation_wrote(fail_env):
    fail_env["present"].add("cancelled_running")
    executor._fail(None, {"id": 5}, "Cancelled by Jordan Ray while it was running.",
                   stopped=("cancelled_running", "U0EXAMPLE001", "Jordan Ray"))
    assert fail_env["logged"] == []


def test_fail_without_a_stop_keeps_the_failed_row(fail_env):
    executor._fail(None, {"id": 5}, "boom")
    assert fail_env["logged"] == [("failed", None, None, {"error": "boom"})]


# ---- the watchdog records its ask before it signals -----------------------

def test_the_watchdog_records_the_ask_before_the_signal(monkeypatch):
    order = []
    monkeypatch.setattr(cancellation.db, "fetch_all", lambda sql, params=None: [{"id": 9}])
    monkeypatch.setattr(cancellation.cfg, "get_int",
                        lambda k, d=None: 420 if k == "execution_runaway_sec" else d)
    monkeypatch.setattr(cancellation, "request_cancel",
                        lambda rid, by_id, by_name: order.append(("ask", rid, by_id, by_name)))
    monkeypatch.setattr(cancellation, "stop_backend",
                        lambda rid: order.append(("signal", rid)) or "terminated")
    monkeypatch.setattr(cancellation, "_fail_request",
                        lambda rid, note, a, n, action: order.append(("fail", action, note)) or True)
    assert cancellation.sweep_runaways() == 1
    assert order[0] == ("ask", 9, "SYSTEM", "runaway watchdog")
    assert order[1] == ("signal", 9)
    assert order[2][1] == "execution_runaway_stopped"
    assert order[2][2].endswith("signalled directly (terminated).")
