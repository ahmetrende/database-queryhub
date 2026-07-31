"""core_decide — the ONE approve/reject/changes state machine.

Both surfaces call into this: the Slack approval buttons and the web admin
panel. It had zero direct tests (17% coverage, the entire body of decide() and
apply_effects() unexecuted), which for the shared join point of the whole
approval model is the wrong place to have no coverage. Four invariants live in
those lines, and every one of them is a security or data-integrity property:

  1. Only approve/reject/changes are decisions.
  2. reject and changes require a non-blank reason (it is what the requester is
     told, and what the audit row records).
  3. A request can be decided exactly once — the UPDATE is guarded by
     `status = 'pending'` and a lost race returns None instead of proceeding.
  4. Approving a future-scheduled request parks it at 'scheduled', not
     'approved' — otherwise the executor runs it immediately and the schedule
     is silently ignored.

Driven with the fake cursor/transaction pattern already used by
tests/test_exec_claim.py: no DB, no Slack.
"""
from datetime import datetime, timedelta, timezone

import pytest

from queryhub import core_decide


class _FakeCur:
    """Records executed SQL and returns queued rows."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _FakeTxn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


ROW = {"id": 7, "requester_slack_id": "U0DEV", "bundle_id": None,
       "decided_by_slack_id": "U0DBA", "decision_reason": None,
       "status": "approved", "tier": "ro"}


@pytest.fixture
def wired(monkeypatch):
    """Patch the DB surface + audit; return a handle to inspect the SQL."""
    state = {"pending": {"scheduled_for": None}, "rows": [ROW], "audit": []}

    cur = _FakeCur(state["rows"])
    monkeypatch.setattr(core_decide.db, "transaction", lambda: _FakeTxn(cur))
    monkeypatch.setattr(core_decide.db, "fetch_one",
                        lambda *a, **k: state["pending"])
    monkeypatch.setattr(core_decide.audit, "log_in",
                        lambda c, rid, bid, bname, action, details=None:
                        state["audit"].append((rid, bid, action, details)))
    state["cur"] = cur
    return state


# ---------------------------------------------------------------- invariant 1


@pytest.mark.parametrize("bad", ["approved", "APPROVE", "yes", "", "delete"])
def test_unknown_decision_is_refused(wired, bad):
    with pytest.raises(ValueError) as e:
        core_decide.decide(7, bad, by_id="U0DBA", by_name="dba")
    assert "decision must be one of" in str(e.value)


# ---------------------------------------------------------------- invariant 2


@pytest.mark.parametrize("decision", ["reject", "changes"])
@pytest.mark.parametrize("reason", [None, "", "   ", "\n\t "])
def test_reject_and_changes_require_a_real_reason(wired, decision, reason):
    """A blank reason means the requester is told "rejected" with no why, and
    the audit row records nothing actionable."""
    with pytest.raises(ValueError) as e:
        core_decide.decide(7, decision, by_id="U0DBA", by_name="dba",
                           reason=reason)
    assert "requires a reason" in str(e.value)


@pytest.mark.parametrize("decision,expected_status,expected_action", [
    ("reject", "rejected", "rejected"),
    ("changes", "changes_requested", "changes_requested"),
])
def test_reject_and_changes_record_status_reason_and_audit(
        wired, decision, expected_status, expected_action):
    out = core_decide.decide(7, decision, by_id="U0DBA", by_name="dba",
                            reason="needs a WHERE clause")
    assert out is not None
    assert out.decision == decision
    assert out.deferred is False

    sql, params = wired["cur"].executed[0]
    assert "UPDATE requests SET status = %s" in sql
    # The write is guarded on 'pending' — this is invariant 3's enforcement.
    assert "AND status = 'pending'" in sql
    assert params[0] == expected_status
    assert params[1] == "needs a WHERE clause"

    assert wired["audit"] == [(7, "U0DBA", expected_action,
                               {"reason": "needs a WHERE clause"})]


# ---------------------------------------------------------------- invariant 3


def test_decide_returns_none_when_the_row_was_already_decided(wired):
    """Two admins hit Approve at once. The second UPDATE matches no row
    (status is no longer 'pending'); decide must report the loss, not fabricate
    an Outcome that would dispatch the query a second time."""
    wired["cur"]._rows = []            # RETURNING yields nothing
    assert core_decide.decide(7, "approve", by_id="U0DBA", by_name="dba") is None
    assert wired["audit"] == [], "audit row written for a decision that lost"


def test_approve_of_a_non_pending_request_never_updates(wired, monkeypatch):
    """The pre-read finds no pending row, so nothing is written at all."""
    monkeypatch.setattr(core_decide.db, "fetch_one", lambda *a, **k: None)
    assert core_decide.decide(7, "approve", by_id="U0DBA", by_name="dba") is None
    assert wired["cur"].executed == []


@pytest.mark.parametrize("decision", ["reject", "changes"])
def test_lost_race_on_reject_and_changes_also_returns_none(wired, decision):
    wired["cur"]._rows = []
    assert core_decide.decide(7, decision, by_id="U0DBA", by_name="dba",
                              reason="why") is None
    assert wired["audit"] == []


# ---------------------------------------------------------------- invariant 4


def test_approving_a_future_scheduled_request_parks_it_as_scheduled(wired):
    """If this wrote 'approved', the executor would pick the row up on the next
    dispatch and run a query the DBA deliberately deferred."""
    future = datetime.now(timezone.utc) + timedelta(hours=6)
    wired["pending"] = {"scheduled_for": future}

    out = core_decide.decide(7, "approve", by_id="U0DBA", by_name="dba")
    assert out is not None and out.deferred is True

    _, params = wired["cur"].executed[0]
    assert params[0] == "scheduled"
    assert wired["audit"][0][3]["deferred"] is True


def test_approving_a_past_scheduled_request_runs_now(wired):
    """A schedule that has already come due is not a deferral — it must go to
    'approved' or the request sits forever waiting for a moment that passed."""
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    wired["pending"] = {"scheduled_for": past}

    out = core_decide.decide(7, "approve", by_id="U0DBA", by_name="dba")
    assert out is not None and out.deferred is False
    assert wired["cur"].executed[0][1][0] == "approved"


def test_plain_approve_has_no_schedule_and_is_not_deferred(wired):
    out = core_decide.decide(7, "approve", by_id="U0DBA", by_name="dba")
    assert out is not None and out.deferred is False
    assert wired["cur"].executed[0][1][0] == "approved"
    assert wired["audit"][0][2] == "approved"
    assert wired["audit"][0][3] == {"scheduled_for": None, "deferred": False}


def test_decider_identity_is_written_to_the_row(wired):
    """The audit trail's actor. Both surfaces pass their own id/name here, and
    a decision with no attributable actor is worse than none."""
    core_decide.decide(7, "approve", by_id="U0DBA", by_name="Ada")
    params = wired["cur"].executed[0][1]
    assert "U0DBA" in params and "Ada" in params


# ------------------------------------------------- apply_effects: dispatch


def test_immediate_approve_dispatches_to_the_executor(monkeypatch):
    """The one effect that must happen exactly once on approve."""
    from queryhub import executor
    from queryhub.slack_app import notifications

    submitted = []
    monkeypatch.setattr(executor, "submit",
                        lambda row, client: submitted.append(row["id"]))
    for name in ("update_all_admin_messages", "dm_requester",
                 "update_requester_card", "update_bundle_admin_dms",
                 "dm_user_scheduled"):
        monkeypatch.setattr(notifications, name, lambda *a, **k: None)
    monkeypatch.setattr(notifications, "requester_card_blocks",
                        lambda *a, **k: [])
    monkeypatch.setattr(notifications, "_status_color", lambda *a, **k: "#000")
    monkeypatch.setattr(notifications, "_tier_color", lambda *a, **k: "#000")

    core_decide.apply_effects(None, core_decide.Outcome(
        row=dict(ROW), decision="approve", deferred=False))
    assert submitted == [7]


def test_deferred_approve_does_not_dispatch(monkeypatch):
    """A scheduled request must wait for the scheduler, not run at decide time."""
    from queryhub import executor
    from queryhub.slack_app import notifications

    submitted = []
    monkeypatch.setattr(executor, "submit",
                        lambda row, client: submitted.append(row["id"]))
    for name in ("update_all_admin_messages", "dm_user_scheduled",
                 "update_bundle_admin_dms"):
        monkeypatch.setattr(notifications, name, lambda *a, **k: None)

    row = dict(ROW)
    row["scheduled_for"] = datetime.now(timezone.utc) + timedelta(hours=2)
    core_decide.apply_effects(None, core_decide.Outcome(
        row=row, decision="approve", deferred=True))
    assert submitted == []


@pytest.mark.parametrize("decision", ["reject", "changes"])
def test_reject_and_changes_never_dispatch(monkeypatch, decision):
    from queryhub import executor, ratings
    from queryhub.slack_app import notifications

    submitted = []
    monkeypatch.setattr(executor, "submit",
                        lambda row, client: submitted.append(row["id"]))
    monkeypatch.setattr(ratings, "maybe_prompt", lambda *a, **k: None)
    for name in ("update_all_admin_messages", "dm_requester",
                 "update_requester_card", "update_bundle_admin_dms"):
        monkeypatch.setattr(notifications, name, lambda *a, **k: None)
    monkeypatch.setattr(notifications, "requester_card_blocks",
                        lambda *a, **k: [])
    monkeypatch.setattr(notifications, "resubmit_action_block",
                        lambda rid: {})
    monkeypatch.setattr(notifications, "_status_color", lambda *a, **k: "#000")
    monkeypatch.setattr(notifications, "_tier_color", lambda *a, **k: "#000")

    row = dict(ROW)
    row["decision_reason"] = "no"
    core_decide.apply_effects(None, core_decide.Outcome(
        row=row, decision=decision, deferred=False))
    assert submitted == []
