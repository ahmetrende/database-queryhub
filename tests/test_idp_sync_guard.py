"""A portal sync that would disable many requesters waits for a super-admin.

The reconcile disables everyone the panel's list leaves out, on a fifteen-minute
timer. A list that is wrong (a role not yet granted, one removed by mistake)
would lock that many people out of Slack, the web and MCP within one tick, and
the only guard was a refusal of an EMPTY list. These pin the state machine
against an in-memory copy of `idp_sync_hold`; the SQL itself is run for real in
test_integration_idp_sync_guard.py.
"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from queryhub import idp_sync_guard as guard
from queryhub.idp_sync_guard import HoldDecisionRefused, Verdict
from queryhub.slack_app import idp_sync_card

PEOPLE = [f"U{n:010d}" for n in range(1, 9)]          # eight people


class Holds:
    """`idp_sync_hold` behind the guard's own queries."""

    def __init__(self):
        self.rows = []
        self.limit = 5
        self.reach = 2                  # super-admins a card gets to
        self.notified = []              # hold ids a card was sent for
        self.touched = []               # hold ids seen again
        self.reminder_due = False

    def _approved_for(self, ids):
        for r in self.rows:
            if (r["status"] == "approved" and not r.get("applied")
                    and set(ids) <= set(r["ids"])):
                return r
        return None

    def usable(self, ids):
        r = self._approved_for(ids)
        return {"id": r["id"]} if r else None

    def claim(self, ids):
        r = self._approved_for(ids)
        if r is None:
            return None
        r["applied"] = True
        return {"id": r["id"]}

    def same(self, ids):
        for r in reversed(self.rows):
            if r["status"] in ("pending", "rejected") and set(r["ids"]) == set(ids):
                return {"id": r["id"], "status": r["status"],
                        "reminder_due": self.reminder_due}
        return None

    def open(self, ids, lim, actor):
        for r in self.rows:
            if r["status"] == "pending":
                r["status"] = "superseded"
        row = {"id": len(self.rows) + 1, "ids": list(ids), "status": "pending",
               "actor": actor}
        self.rows.append(row)
        return {"id": row["id"]}

    def notify(self, hold_id):
        self.notified.append(hold_id)
        return self.reach

    def decide(self, hold_id, status):
        next(r for r in self.rows if r["id"] == hold_id)["status"] = status


@pytest.fixture
def holds(monkeypatch):
    h = Holds()
    monkeypatch.setattr(guard.cfg, "get_int", lambda key, default: h.limit)
    monkeypatch.setattr(guard, "_usable_approval", h.usable)
    monkeypatch.setattr(guard, "_claim_approval", h.claim)
    monkeypatch.setattr(guard, "_same_list", h.same)
    monkeypatch.setattr(guard, "_open", h.open)
    monkeypatch.setattr(guard, "_notify", h.notify)
    monkeypatch.setattr(guard.db, "execute",
                        lambda sql, params=None: h.touched.append(params[0]))
    return h


# ---- the limit -------------------------------------------------------------

def test_the_limit_is_five_out_of_the_box():
    assert guard.DEFAULT_LIMIT == 5
    assert guard.limit() == 5


@pytest.mark.parametrize("raw", [-1, -50])
def test_a_negative_limit_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setattr(guard.cfg, "get_int", lambda key, default: raw)
    assert guard.limit() == 5


def test_a_limit_that_is_not_a_number_falls_back_to_the_default(monkeypatch):
    def junk(key, default):
        raise ValueError("invalid literal for int()")
    monkeypatch.setattr(guard.cfg, "get_int", junk)
    assert guard.limit() == 5


def test_zero_is_a_limit_and_means_every_disabling_run_asks(holds):
    holds.limit = 0
    v = guard.gate(PEOPLE[:1])
    assert v.state == "pending" and holds.notified == [1]


# ---- a run within the limit ------------------------------------------------

def test_five_people_is_within_the_limit_and_asks_nobody(holds):
    v = guard.gate(PEOPLE[:5])
    assert v == Verdict("within_limit", 5, 5)
    assert v.allowed
    assert holds.rows == [] and holds.notified == []


def test_a_run_that_disables_nobody_is_within_the_limit(holds):
    assert guard.gate([]).allowed and holds.rows == []


# ---- a run over the limit --------------------------------------------------

def test_six_people_is_held_and_the_super_admins_are_asked_once(holds):
    v = guard.gate(PEOPLE[:6], actor="U_SYNC")
    assert (v.state, v.count, v.limit, v.hold_id, v.notified) == ("pending", 6, 5, 1, 2)
    assert not v.allowed
    assert holds.notified == [1]
    assert holds.rows[0]["actor"] == "U_SYNC"


def test_the_ids_are_sorted_and_deduplicated_before_they_are_stored(holds):
    guard.gate(PEOPLE[:6][::-1] + PEOPLE[:2])
    assert holds.rows[0]["ids"] == PEOPLE[:6]


def test_the_same_list_arriving_again_does_not_ask_again(holds):
    """The panel repeats itself every fifteen minutes; a card per tick would
    bury the one that matters."""
    guard.gate(PEOPLE[:6])
    v = guard.gate(PEOPLE[:6])
    assert v.state == "pending" and v.hold_id == 1 and v.notified == 0
    assert holds.notified == [1] and len(holds.rows) == 1
    assert holds.touched == [1]


def test_an_unanswered_card_is_sent_again_after_a_day(holds):
    guard.gate(PEOPLE[:6])
    holds.reminder_due = True
    v = guard.gate(PEOPLE[:6])
    assert v.state == "pending" and v.notified == 2
    assert holds.notified == [1, 1] and len(holds.rows) == 1


def test_a_different_list_is_a_new_question_and_supersedes_the_old_one(holds):
    guard.gate(PEOPLE[:6])
    v = guard.gate(PEOPLE[:7])
    assert v.hold_id == 2 and holds.notified == [1, 2]
    assert [r["status"] for r in holds.rows] == ["superseded", "pending"]


def test_nobody_reachable_still_holds_the_run(holds):
    """No Slack, or no super-admin to send it to: fail closed. The run stays
    held until someone raises the limit."""
    holds.reach = 0
    v = guard.gate(PEOPLE[:6])
    assert not v.allowed and v.notified == 0


# ---- a decision ------------------------------------------------------------

def test_an_approval_lets_the_next_run_through_once(holds):
    guard.gate(PEOPLE[:6])
    holds.decide(1, "approved")
    first = guard.gate(PEOPLE[:6])
    assert first.state == "approved" and first.allowed and first.hold_id == 1
    # Spent. If the same list comes back, that is a new question.
    again = guard.gate(PEOPLE[:6])
    assert again.state == "pending" and again.hold_id == 2


def test_an_approval_covers_a_smaller_list_inside_it(holds):
    """Someone got their role between the yes and the next tick."""
    holds.limit = 2
    guard.gate(PEOPLE[:5])
    holds.decide(1, "approved")
    assert guard.gate(PEOPLE[:3]).state == "approved"


def test_an_approval_does_not_cover_someone_who_was_not_on_the_card(holds):
    holds.limit = 2
    guard.gate(PEOPLE[:5])
    holds.decide(1, "approved")
    v = guard.gate(PEOPLE[:2] + [PEOPLE[7]])
    assert v.state == "pending" and v.hold_id == 2


def test_a_rejected_list_stays_blocked_and_nobody_is_asked_again(holds):
    guard.gate(PEOPLE[:6])
    holds.decide(1, "rejected")
    v = guard.gate(PEOPLE[:6])
    assert v.state == "rejected" and not v.allowed
    assert holds.notified == [1]


# ---- a dry run -------------------------------------------------------------

def test_a_dry_run_at_the_limit_is_within_it(holds):
    v = guard.preview(PEOPLE[:5])
    assert v == Verdict("within_limit", 5, 5) and v.allowed


def test_a_dry_run_reports_and_writes_nothing(holds):
    v = guard.preview(PEOPLE[:6])
    assert v.state == "would_hold" and not v.allowed
    assert holds.rows == [] and holds.notified == [] and holds.touched == []


def test_a_dry_run_sees_a_pending_an_approved_and_a_rejected_list(holds):
    guard.gate(PEOPLE[:6])
    assert guard.preview(PEOPLE[:6]).state == "pending"
    holds.decide(1, "approved")
    assert guard.preview(PEOPLE[:6]).state == "approved"
    assert holds.rows[0].get("applied") is None          # looking is not spending
    guard.gate(PEOPLE[:7])
    holds.decide(2, "rejected")
    assert guard.preview(PEOPLE[:7]).state == "rejected"


# ---- the words -------------------------------------------------------------

def test_the_refusal_says_how_many_the_limit_and_that_nothing_changed():
    m = Verdict("pending", 5, 35, 1).message()
    assert "35 requesters" in m and "limit of 5" in m
    assert "asked to approve" in m and "Nothing was changed" in m


def test_a_rejected_refusal_says_how_to_move_on():
    m = Verdict("rejected", 5, 35, 1).message()
    assert "rejected" in m and guard.LIMIT_KEY in m


def test_the_verdict_serialises_for_the_response():
    assert Verdict("pending", 5, 8, 3).as_dict() == {
        "state": "pending", "limit": 5, "would_disable": 8, "hold_id": 3}
    assert Verdict("within_limit", 5, 2).as_dict() == {
        "state": "within_limit", "limit": 5, "would_disable": 2}


# ---- deciding --------------------------------------------------------------

class Cursor:
    def __init__(self, row):
        self.row, self.executed = row, []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.row


@pytest.fixture
def deciding(monkeypatch):
    st = SimpleNamespace(row={"id": 4, "would_disable": PEOPLE[:6], "limit_at_hold": 5,
                              "cards": [], "status": "approved"},
                         cursors=[], current={"status": "approved"})

    @contextmanager
    def transaction():
        cur = Cursor(st.row)
        st.cursors.append(cur)
        yield cur

    monkeypatch.setattr(guard.db, "transaction", transaction)
    monkeypatch.setattr(guard.admins, "is_super_admin", lambda uid: uid == "U_SUPER")
    monkeypatch.setattr(guard, "get", lambda hold_id: st.current)
    return st


def test_only_a_super_admin_may_decide(deciding):
    with pytest.raises(HoldDecisionRefused) as e:
        guard.decide(4, approve=True, actor_id="U_ADMIN", actor_name="An Admin")
    assert e.value.status == 403
    assert deciding.cursors == []                      # nothing was even opened


def test_approving_records_who_and_writes_an_audit_row(deciding):
    row = guard.decide(4, approve=True, actor_id="U_SUPER", actor_name="Sam")
    assert row["would_disable"] == PEOPLE[:6]
    sql, params = deciding.cursors[0].executed[0]
    assert "status = 'pending'" in sql
    assert params == ("approved", "U_SUPER", "Sam", 4)
    audit_sql, audit_params = deciding.cursors[0].executed[1]
    assert "audit_log" in audit_sql
    assert "idp_sync_hold_approved" in audit_params


def test_rejecting_is_a_decision_too(deciding):
    guard.decide(4, approve=False, actor_id="U_SUPER", actor_name="Sam")
    assert deciding.cursors[0].executed[0][1][0] == "rejected"
    assert "idp_sync_hold_rejected" in deciding.cursors[0].executed[1][1]


def test_a_second_super_admin_finds_it_already_decided(deciding):
    deciding.row = None
    deciding.current = {"status": "approved"}
    with pytest.raises(HoldDecisionRefused) as e:
        guard.decide(4, approve=False, actor_id="U_SUPER", actor_name="Sam")
    assert e.value.status == 409 and "Already approved" in e.value.message


def test_an_unknown_hold_is_a_404(deciding):
    deciding.row = None
    deciding.current = None
    with pytest.raises(HoldDecisionRefused) as e:
        guard.decide(99, approve=True, actor_id="U_SUPER", actor_name="Sam")
    assert e.value.status == 404


# ---- the card --------------------------------------------------------------

class Slack:
    def __init__(self, fail_for=()):
        self.fail_for, self.opened = set(fail_for), []

    def conversations_open(self, users):
        if users in self.fail_for:
            raise RuntimeError("cannot_dm_bot")
        self.opened.append(users)
        return {"channel": {"id": "D" + users}}


@pytest.fixture
def cards(monkeypatch):
    st = SimpleNamespace(posted=[], writes=[], slack=Slack())
    hold = {"id": 7, "would_disable": PEOPLE[:6], "limit_at_hold": 5}
    admins_ = [{"slack_user_id": "U_SUPER_A"}, {"slack_user_id": "U_SCOPED"},
               {"slack_user_id": "U_SUPER_B"}]
    monkeypatch.setattr(guard, "_client", lambda: st.slack)
    monkeypatch.setattr(guard, "get", lambda hold_id: hold)
    monkeypatch.setattr(guard.admins, "list_active", lambda: admins_)
    monkeypatch.setattr(guard.admins, "is_super_admin", lambda uid: "SUPER" in uid)
    monkeypatch.setattr(guard.db, "execute",
                        lambda sql, params=None: st.writes.append(params))

    def post(client, **kw):
        st.posted.append(kw)
        return {"ts": f"1.{len(st.posted)}"}
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "_post", post)
    return st


def test_the_card_goes_to_super_admins_only(cards):
    assert guard._notify(7) == 2
    assert cards.slack.opened == ["U_SUPER_A", "U_SUPER_B"]
    assert [p["channel"] for p in cards.posted] == ["DU_SUPER_A", "DU_SUPER_B"]


def test_where_each_card_went_is_remembered_so_a_decision_can_close_them(cards):
    import json
    guard._notify(7)
    stored = json.loads(cards.writes[0][0])
    assert stored == [{"user": "U_SUPER_A", "channel": "DU_SUPER_A", "ts": "1.1"},
                      {"user": "U_SUPER_B", "channel": "DU_SUPER_B", "ts": "1.2"}]
    assert cards.writes[0][1] == 7


def test_one_super_admin_who_cannot_be_reached_does_not_stop_the_rest(cards):
    cards.slack.fail_for = {"U_SUPER_A"}
    assert guard._notify(7) == 1
    assert cards.slack.opened == ["U_SUPER_B"]


def test_without_a_slack_client_no_card_is_sent_and_nothing_breaks(cards, monkeypatch):
    monkeypatch.setattr(guard, "_client", lambda: None)
    assert guard._notify(7) == 0
    assert cards.posted == []


def test_the_card_asks_the_question_and_offers_both_answers():
    hold = {"id": 7, "would_disable": PEOPLE[:6], "limit_at_hold": 5}
    blocks = idp_sync_card.blocks(hold)
    text = " ".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    assert "wants to disable 6 people" in text and "limit of 5" in text
    assert "Do you approve?" in text
    assert all(f"<@{p}>" in text for p in PEOPLE[:6])
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    assert [(a["action_id"], a["value"]) for a in actions] == [
        (idp_sync_card.ACTION_APPROVE, "7"), (idp_sync_card.ACTION_REJECT, "7")]


def test_a_long_list_is_cut_to_a_readable_card():
    ids = [f"U{n:010d}" for n in range(40)]
    hold = {"id": 1, "would_disable": ids, "limit_at_hold": 5}
    text = idp_sync_card.blocks(hold)[1]["text"]["text"]
    assert text.count("<@") == 25 and text.endswith("and 15 more")
