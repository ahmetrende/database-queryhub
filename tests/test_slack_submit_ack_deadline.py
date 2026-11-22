"""The /sql modal submit answers Slack within its 3-second deadline.

The submit checks can download a file and reach the target (EXPLAIN, DDL
dependents). They run with a time budget:

- checks that finish in time behave as before: inline errors, or ack() and
  submit;
- checks that run late change the modal to a "Checking" view, and the
  request is then submitted or refused in that view (a DM when the view is
  gone);
- a refusal is recorded in submission_failures after the ack, never before.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from queryhub import core_submit
from queryhub.slack_app import handlers, modal

USER = "U0EXAMPLE001"
BODY = {"user": {"id": USER, "name": "ex"},
        "view": {"id": "V0EXAMPLE", "hash": "h-old", "private_metadata": "{}"}}
PARSED = {"query": "SELECT 1 FROM orders", "query_file_id": None,
          "target_server_id": 7, "database_name": "ledger",
          "justification": None, "wants_result": True, "result_format": "csv",
          "schedule_date": None, "schedule_time": None, "req_id": None}
PREP = SimpleNamespace(target=SimpleNamespace(id=7), database="ledger",
                       required_mode="ro")


class _Client:
    def __init__(self, events, fail_update=False):
        self.events = events
        self.fail_update = fail_update

    def views_update(self, **kwargs):
        if self.fail_update:
            raise RuntimeError("not_found")
        self.events.append(("views_update", kwargs))


@pytest.fixture
def run(monkeypatch):
    """Run handle_view_submission with every collaborator below Slack faked.
    `go(validate, ...)` returns the ordered list of events."""
    events: list = []
    monkeypatch.setattr(handlers, "_ACK_BUDGET_SEC", 0.2)
    monkeypatch.setattr(handlers.entry_gate, "is_open", lambda uid: True)
    monkeypatch.setattr(handlers.modal, "parse_submission", lambda view: dict(PARSED))
    monkeypatch.setattr(handlers, "_log_submission_failure",
                        lambda body, user, mode, errors: events.append(("log", errors)))
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda client, uid, text=None, **k: events.append(("dm", text)))

    def create(prep, supersedes_id=None, draft_id=None):
        events.append(("create", prep))
        return SimpleNamespace(row={"id": 42}, superseded_row=None)
    monkeypatch.setattr(handlers.core_submit, "create_request", create)
    monkeypatch.setattr(handlers.core_submit, "dispatch_and_notify",
                        lambda client, prep, outcome, dm_requester=True: "dispatched")

    def go(validate, fail_update=False):
        monkeypatch.setattr(handlers.core_submit, "validate_submission", validate)
        started = time.monotonic()

        def ack(payload=None):
            events.append(("ack", payload, time.monotonic() - started))
        handlers.handle_view_submission(ack, BODY, _Client(events, fail_update))
        return events

    return go


def _slow(result, seconds=0.6):
    def validate(*a, **k):
        time.sleep(seconds)
        if isinstance(result, Exception):
            raise result
        return result
    return validate


def _acks(events):
    return [e for e in events if e[0] == "ack"]


def _kinds(events):
    return [e[0] for e in events]


def _view_text(event):
    return event[1]["view"]["blocks"][0]["text"]["text"]


# --- the fast path is unchanged ---------------------------------------------------

def test_fast_checks_ack_plainly_and_submit(run):
    events = run(lambda *a, **k: PREP)
    assert [a[1] for a in _acks(events)] == [None]
    assert ("create", PREP) in events
    assert "views_update" not in _kinds(events)


def test_a_fast_refusal_shows_inline_and_is_logged_after_the_ack(run):
    events = run(lambda *a, **k: core_submit.Rejection("query", "Not allowed."))
    assert [a[1] for a in _acks(events)] == [
        {"response_action": "errors", "errors": {modal.B_QUERY: "Not allowed."}}]
    assert _kinds(events) == ["ack", "log"]


# --- slow checks ------------------------------------------------------------------

def test_slow_checks_ack_within_the_budget_and_then_submit(run):
    events = run(_slow(PREP))
    first = _acks(events)[0]
    assert first[1] == {"response_action": "update",
                        "view": handlers._status_view(handlers._CHECKING_TEXT)}
    assert first[2] < 0.2 + 0.3, "the ack waited for the slow checks"
    assert len(_acks(events)) == 1
    assert ("create", PREP) in events
    update = [e for e in events if e[0] == "views_update"][-1]
    assert update[1]["view_id"] == "V0EXAMPLE"
    assert "hash" not in update[1], "the body hash is stale after the update ack"
    assert _view_text(update) == handlers._SUBMITTED_TEXT


def test_a_late_refusal_reaches_the_user_in_the_modal(run):
    events = run(_slow(core_submit.Rejection("query", "EXPLAIN failed: no table.")))
    assert _acks(events)[0][1]["response_action"] == "update"
    assert "create" not in _kinds(events)
    assert ("log", {modal.B_QUERY: "EXPLAIN failed: no table."}) in events
    update = [e for e in events if e[0] == "views_update"][-1]
    assert "EXPLAIN failed: no table." in _view_text(update)
    assert "dm" not in _kinds(events)


def test_a_late_refusal_is_sent_by_dm_when_the_modal_is_gone(run):
    events = run(_slow(core_submit.Rejection("query", "EXPLAIN failed: no table.")),
                 fail_update=True)
    dms = [e[1] for e in events if e[0] == "dm"]
    assert len(dms) == 1 and "EXPLAIN failed: no table." in dms[0]


def test_a_late_crash_still_tells_the_user(run):
    events = run(_slow(RuntimeError("target timeout")))
    assert "create" not in _kinds(events)
    update = [e for e in events if e[0] == "views_update"][-1]
    assert "could not check your query" in _view_text(update)
