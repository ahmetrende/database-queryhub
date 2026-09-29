"""The Approve / Reject buttons on the card a held IDP sync sends (idp_sync_guard).

The decision is `idp_sync_guard.decide`; these pin what the handler does around
it: close the card the admin clicked and the same card in every other
super-admin's DM, tell a non-super-admin no, and survive a card Slack will not
edit, because the decision is already recorded by then.
"""
from types import SimpleNamespace

import pytest

from queryhub.slack_app import handlers, idp_sync_card, notifications

PEOPLE = [f"U{n:010d}" for n in range(1, 7)]
ROW = {"id": 7, "would_disable": PEOPLE, "limit_at_hold": 5, "status": "approved",
       "cards": [{"user": "U_A", "channel": "D_A", "ts": "1.1"},
                 {"user": "U_B", "channel": "D_B", "ts": "1.2"}]}


def _body(actor="U_SUPER", value="7", channel="D_A", ts="1.1"):
    return {"user": {"id": actor, "name": "sam"}, "actions": [{"value": value}],
            "container": {"channel_id": channel, "message_ts": ts}}


@pytest.fixture
def slack(monkeypatch):
    st = SimpleNamespace(acked=0, updates=[], dms=[], decisions=[], row=ROW,
                         refuse=None, current=None, fail_update_for=None)

    def decide(hold_id, *, approve, actor_id, actor_name):
        st.decisions.append((hold_id, approve, actor_id, actor_name))
        if st.refuse:
            raise st.refuse
        return st.row

    def update(client, **kw):
        if kw["channel"] == st.fail_update_for:
            raise RuntimeError("message_not_found")
        st.updates.append(kw)

    monkeypatch.setattr(handlers.idp_sync_guard, "decide", decide)
    monkeypatch.setattr(handlers.idp_sync_guard, "get", lambda hold_id: st.current)
    monkeypatch.setattr(notifications, "_update", update)
    monkeypatch.setattr(notifications, "dm_requester",
                        lambda client, uid, text: st.dms.append((uid, text)))
    st.ack = lambda: setattr(st, "acked", st.acked + 1)
    return st


def _text(update):
    return update["blocks"][0]["text"]["text"]


def test_approving_records_the_decision_and_closes_every_card(slack):
    handlers.handle_idp_sync_approve(slack.ack, _body(), object())
    assert slack.acked == 1
    assert slack.decisions == [(7, True, "U_SUPER", "sam")]
    assert sorted((u["channel"], u["ts"]) for u in slack.updates) == [
        ("D_A", "1.1"), ("D_B", "1.2")]
    assert all("Approved" in _text(u) and "<@U_SUPER>" in _text(u) for u in slack.updates)
    assert all(b["type"] == "section" for u in slack.updates for b in u["blocks"])


def test_rejecting_says_so_on_every_card(slack):
    handlers.handle_idp_sync_reject(slack.ack, _body(), object())
    assert slack.decisions == [(7, False, "U_SUPER", "sam")]
    assert len(slack.updates) == 2
    assert all("Rejected" in _text(u) for u in slack.updates)


def test_a_card_missing_from_the_stored_list_is_still_closed(slack):
    """The clicked message is known from the click itself."""
    slack.row = {**ROW, "cards": []}
    handlers.handle_idp_sync_approve(slack.ack, _body(), object())
    assert [(u["channel"], u["ts"]) for u in slack.updates] == [("D_A", "1.1")]


def test_the_clicked_card_is_edited_once_even_though_it_is_also_stored(slack):
    handlers.handle_idp_sync_approve(slack.ack, _body(), object())
    assert [u["channel"] for u in slack.updates].count("D_A") == 1


def test_a_non_super_admin_is_told_no_and_no_card_changes(slack):
    slack.refuse = handlers.idp_sync_guard.HoldDecisionRefused("Super-admin access required.", 403)
    handlers.handle_idp_sync_approve(slack.ack, _body(actor="U_ADMIN"), object())
    assert slack.acked == 1
    assert slack.updates == []
    assert slack.dms and slack.dms[0][0] == "U_ADMIN"
    assert "super-admin" in slack.dms[0][1]


def test_a_request_someone_else_already_decided_says_so_on_the_card(slack):
    slack.refuse = handlers.idp_sync_guard.HoldDecisionRefused("Already approved.")
    slack.current = {"status": "approved"}
    handlers.handle_idp_sync_reject(slack.ack, _body(), object())
    assert len(slack.updates) == 1
    assert _text(slack.updates[0]) == idp_sync_card.already_text("approved")


def test_a_request_that_is_gone_says_so_instead_of_crashing(slack):
    slack.refuse = handlers.idp_sync_guard.HoldDecisionRefused("No such request.", 404)
    slack.current = None
    handlers.handle_idp_sync_approve(slack.ack, _body(), object())
    assert "no longer available" in _text(slack.updates[0])


def test_a_card_slack_will_not_edit_does_not_undo_the_decision(slack):
    slack.fail_update_for = "D_B"
    handlers.handle_idp_sync_approve(slack.ack, _body(), object())    # must not raise
    assert slack.decisions and [u["channel"] for u in slack.updates] == ["D_A"]


@pytest.mark.parametrize("body", [
    {"user": {"id": "U_SUPER"}, "actions": [], "container": {}},
    {"user": {"id": "U_SUPER"}, "actions": [{"value": "not-a-number"}],
     "container": {"channel_id": "D", "message_ts": "1"}},
    {"user": {"id": "U_SUPER"}, "actions": [{"value": "7"}]},
])
def test_a_malformed_click_is_acknowledged_and_ignored(slack, body):
    handlers.handle_idp_sync_approve(slack.ack, body, object())
    assert slack.acked == 1 and slack.decisions == [] and slack.updates == []


def test_both_buttons_are_wired_to_their_handlers():
    seen = {}

    class App:
        def __getattr__(self, name):
            def register(*args, **kwargs):
                def wrap(fn):
                    if name == "action":
                        seen[args[0]] = fn
                    return fn
                return wrap
            return register

    handlers.register(App())
    assert seen[idp_sync_card.ACTION_APPROVE] is handlers.handle_idp_sync_approve
    assert seen[idp_sync_card.ACTION_REJECT] is handlers.handle_idp_sync_reject
