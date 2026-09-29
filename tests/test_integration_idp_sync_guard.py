"""The held-sync guard's SQL, run against a real database (migration 138).

The state machine is pinned with an in-memory copy of the table in
test_idp_sync_guard.py. What that cannot show is that the queries mean what the
copy assumes: array containment for "inside the approved set", the 24-hour
windows, and the single-statement claim that spends an approval. Those are SQL,
so they run here.

Every row is keyed to ids no real person has, and only rows overlapping them are
removed, so a stray run against a live database leaves real holds alone. The audit
log is append-only, so its rows stay behind.
"""
import os

import pytest

from queryhub import db
from queryhub import idp_sync_guard as guard

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

IDS = [f"U0GUARD{n:03d}" for n in range(1, 9)]          # eight people that do not exist
SUPER = "U0GUARDSUP"


def _wipe():
    db.execute("DELETE FROM idp_sync_hold WHERE would_disable && %s::text[]", (IDS,))


@pytest.fixture(autouse=True)
def clean_slate(monkeypatch):
    _wipe()
    monkeypatch.setattr(guard, "limit", lambda: 5)
    monkeypatch.setattr(guard, "_client", lambda: None)           # no Slack here
    monkeypatch.setattr(guard.admins, "is_super_admin", lambda uid: uid == SUPER)
    yield
    _wipe()


def _rows():
    return db.fetch_all("SELECT id, status, would_disable, applied_at FROM idp_sync_hold "
                        " WHERE would_disable && %s::text[] ORDER BY id", (IDS,))


def _approve(hold_id):
    return guard.decide(hold_id, approve=True, actor_id=SUPER, actor_name="Sam")


def test_the_table_and_the_setting_exist():
    assert db.fetch_one("SELECT to_regclass('idp_sync_hold') AS t")["t"] is not None
    row = db.fetch_one("SELECT value FROM bot_config WHERE key = 'idp_sync_max_disable'")
    assert row is not None and row["value"].isdigit()


def test_a_run_within_the_limit_writes_nothing():
    assert guard.gate(IDS[:5]).state == "within_limit"
    assert _rows() == []


def test_the_first_run_over_the_limit_opens_one_sorted_hold():
    v = guard.gate(list(reversed(IDS[:6])), actor=SUPER)
    assert v.state == "pending" and v.notified == 0           # nobody reachable: still held
    rows = _rows()
    assert len(rows) == 1 and rows[0]["would_disable"] == IDS[:6]
    assert rows[0]["status"] == "pending"


def test_the_same_list_again_finds_the_same_hold():
    first = guard.gate(IDS[:6])
    second = guard.gate(IDS[:6])
    assert second.state == "pending" and second.hold_id == first.hold_id
    assert len(_rows()) == 1


def test_a_different_list_supersedes_the_undecided_one():
    guard.gate(IDS[:6])
    guard.gate(IDS[:7])
    assert [r["status"] for r in _rows()] == ["superseded", "pending"]


def test_an_approval_is_spent_by_the_first_run_that_uses_it():
    hold = guard.gate(IDS[:6]).hold_id
    _approve(hold)
    assert guard.preview(IDS[:6]).state == "approved"          # looking spends nothing
    assert guard.gate(IDS[:6]).state == "approved"
    assert guard.gate(IDS[:6]).state == "pending"              # used up: a new question
    assert _rows()[0]["applied_at"] is not None


def test_an_approval_covers_a_smaller_list_inside_it_but_not_a_stranger():
    hold = guard.gate(IDS[:7]).hold_id
    _approve(hold)
    assert guard.gate(IDS[:6]).state == "approved"             # inside the approved set
    hold2 = guard.gate(IDS[:7]).hold_id
    _approve(hold2)
    assert guard.gate(IDS[:6] + [IDS[7]]).state == "pending"   # IDS[7] was not on the card


def test_a_rejected_list_stays_rejected_and_is_not_reopened():
    hold = guard.gate(IDS[:6]).hold_id
    guard.decide(hold, approve=False, actor_id=SUPER, actor_name="Sam")
    assert guard.gate(IDS[:6]).state == "rejected"
    assert len(_rows()) == 1


def test_an_approval_expires_after_a_day():
    hold = guard.gate(IDS[:6]).hold_id
    _approve(hold)
    db.execute("UPDATE idp_sync_hold SET decided_at = NOW() - INTERVAL '25 hours' "
               "WHERE id = %s", (hold,))
    assert guard.gate(IDS[:6]).state == "pending"


def test_a_list_unseen_for_a_day_is_a_new_incident():
    hold = guard.gate(IDS[:6]).hold_id
    db.execute("UPDATE idp_sync_hold SET last_held_at = NOW() - INTERVAL '25 hours' "
               "WHERE id = %s", (hold,))
    assert guard.gate(IDS[:6]).hold_id != hold


def test_deciding_twice_finds_it_decided_and_the_audit_row_names_the_admin():
    hold = guard.gate(IDS[:6]).hold_id
    _approve(hold)
    with pytest.raises(guard.HoldDecisionRefused) as e:
        guard.decide(hold, approve=False, actor_id=SUPER, actor_name="Sam")
    assert "Already approved" in e.value.message
    audited = db.fetch_one("SELECT actor_slack_id FROM audit_log "
                           " WHERE action = 'idp_sync_hold_approved' "
                           "   AND details->>'hold_id' = %s ORDER BY id DESC LIMIT 1",
                           (str(hold),))
    assert audited["actor_slack_id"] == SUPER
