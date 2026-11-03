"""/sql grant can carry auto-approve for read-only queries.

One optional checkbox on the grant modal. Ticked, every grants.grant() call
the submission makes also writes the RO waiver, in the grant's own
transaction; the grantee's DM and the admin's summary say so, and the summary
names a waiver that was skipped because one already covers it.

The waiver takes the access's end ("Access ends"), and an auto-approve rule
needs an end no later than `auto_approve_max_days` from now. So a ticked box
needs an end other than "No end date".

No Slack, no DB: the handler is called with a fake ack and client.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from queryhub import auto_approve_limits
from queryhub.slack_app import admin_grant as ag
from queryhub.slack_app import handlers

ADMIN = "U0EXAMPLE001"


def _block(modal, block_id):
    return next(b for b in modal["blocks"] if b.get("block_id") == block_id)


# --- the modal ---------------------------------------------------------------

def test_the_modal_offers_one_optional_read_only_box():
    blk = _block(ag.grant_modal(allowed_tiers=["ro", "rw"]), ag.B_AUTO)
    assert blk["type"] == "input" and blk["optional"] is True
    el = blk["element"]
    assert el["type"] == "checkboxes" and el["action_id"] == ag.A_AUTO
    assert [o["value"] for o in el["options"]] == ["ro"]
    assert el["options"][0]["text"]["text"] == "Auto-approve read-only queries"
    assert "initial_options" not in el                       # never ticked by default


def test_a_rebuild_keeps_the_box_ticked():
    el = _block(ag.grant_modal(allowed_tiers=["ro"], auto_approve=True), ag.B_AUTO)["element"]
    # Slack matches an initial option by the whole object, not by its value.
    assert el["initial_options"] == el["options"]


def _state(auto=True, tier="rw", users=("U0EXAMPLE002",), targets=(7, 8), ends="30"):
    return {
        ag.B_USER: {ag.A_USER: {"selected_users": list(users)}},
        ag.B_TARGET: {ag.A_TARGET: {"selected_options": [{"value": str(t)} for t in targets]}},
        ag.B_TIER: {ag.A_TIER: {"selected_option": {"value": tier}}},
        ag.B_DBS: {ag.A_DBS: {"selected_options": []}},
        ag.B_REASON: {ag.A_REASON: {"value": "onboarding"}},
        ag.B_ENDS: {ag.A_ENDS: {"selected_option": {"value": ends}}},
        ag.B_AUTO: {ag.A_AUTO: {"selected_options": [{"value": "ro"}] if auto else []}},
    }


def test_the_state_reader():
    assert ag.auto_approve_tier(_state(auto=True)) == "ro"
    assert ag.auto_approve_tier(_state(auto=False)) is None
    assert ag.auto_approve_tier({}) is None                  # a view built before the box


# --- the end field -----------------------------------------------------------

def _values(block):
    return [o["value"] for o in block["element"]["options"]]


def test_the_modal_asks_when_the_access_ends():
    blk = _block(ag.grant_modal(allowed_tiers=["ro"]), ag.B_ENDS)
    assert blk["type"] == "input" and not blk.get("optional")
    assert blk["element"]["type"] == "static_select"
    assert blk["element"]["action_id"] == ag.A_ENDS
    assert _values(blk) == ["none", "7", "30", "90"]
    # A plain grant keeps what it had: no end unless the admin picks one.
    assert blk["element"]["initial_option"]["value"] == "none"
    assert "90 days" in blk["hint"]["text"]


def test_the_end_picks_follow_the_rule_limit():
    """The web form's picks: 7, 30 and 90 days below the limit, and the limit."""
    assert _values(_block(ag.grant_modal(allowed_tiers=["ro"], max_days=20), ag.B_ENDS)) \
        == ["none", "7", "20"]
    assert _values(_block(ag.grant_modal(allowed_tiers=["ro"], max_days=365), ag.B_ENDS)) \
        == ["none", "7", "30", "90", "365"]
    assert _values(_block(ag.grant_modal(allowed_tiers=["ro"], max_days=5), ag.B_ENDS)) \
        == ["none", "5"]


def test_a_rebuild_keeps_the_picked_end():
    el = _block(ag.grant_modal(allowed_tiers=["ro"], ends="30"), ag.B_ENDS)["element"]
    assert el["initial_option"]["value"] == "30"
    # A pick that the limit no longer offers falls back to no end.
    el = _block(ag.grant_modal(allowed_tiers=["ro"], ends="90", max_days=20), ag.B_ENDS)["element"]
    assert el["initial_option"]["value"] == "none"


def test_the_end_reader():
    assert ag.ends_days(_state(ends="30")) == 30
    assert ag.ends_days(_state(ends="none")) is None
    assert ag.ends_days({}) is None                          # a view built before the field
    assert ag.ends_days(_state(ends="0")) is None
    assert ag.ends_days(_state(ends="soon")) is None


def test_no_optional_label_says_optional_itself():
    """Slack adds "(optional)" to the label of an optional input. A label that
    says it too reads "Reason (optional) (optional)"."""
    for b in ag.grant_modal(allowed_tiers=["ro"])["blocks"]:
        if b.get("optional"):
            assert "optional" not in b["label"]["text"].lower(), b["block_id"]


# --- the submission ----------------------------------------------------------

@pytest.fixture
def slack(monkeypatch):
    st = SimpleNamespace(calls=[], dms=[], notified=[], acks=[], outcome=None)

    def grant(**kw):
        st.calls.append(kw)
        out = st.outcome(kw) if st.outcome else None
        return {"mode": kw["mode"], "databases": None, "whitelisted_now": False,
                "auto_approve": out}

    monkeypatch.setattr(auto_approve_limits, "max_days", lambda: 90)
    monkeypatch.setattr(handlers.grants, "authz", lambda uid: {"super": True, "max_tier": None})
    monkeypatch.setattr(handlers.grants, "control_plane_target_ids", lambda: {99})
    monkeypatch.setattr(handlers.grants, "grant", grant)
    monkeypatch.setattr(handlers.grants, "notify_grantee",
                        lambda gid, *a, **k: st.notified.append((gid, k.get("auto_approve_tier"))))
    monkeypatch.setattr(handlers, "_granter_scope_target_ids", lambda uid: None)
    monkeypatch.setattr(handlers, "_slack_profile", lambda client, uid: {})
    monkeypatch.setattr(handlers.targets, "get",
                        lambda tid: SimpleNamespace(alias={7: "prod-orders", 8: "prod-ledger"}[tid]))
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda client, uid, text: st.dms.append((uid, text)))
    st.ack = lambda payload=None: st.acks.append(payload)
    return st


def _submit(slack, **kw):
    body = {"user": {"id": ADMIN, "name": "admin"},
            "view": {"state": {"values": _state(**kw)}}}
    handlers.handle_grant_submission(slack.ack, body, object())


def _written(kw):
    return {"tier": "ro", "written": [{"id": "1", "database": None}], "skipped": []}


def _close_to(at, days):
    want = datetime.now(timezone.utc) + timedelta(days=days)
    return abs((at - want).total_seconds()) < 60


def test_a_ticked_box_with_an_end_writes_the_grant_and_the_waiver(slack):
    """The bug this closes: the box was refused on every submit, because the
    modal had no end field. Now the access and its waiver end on the pick."""
    slack.outcome = _written
    _submit(slack, ends="30")
    assert slack.acks == [None]
    assert len(slack.calls) == 2                              # one per target
    for call in slack.calls:
        assert call["auto_approve_tier"] == "ro"
        assert _close_to(call["expires_at"], 30)
    assert slack.notified == [("U0EXAMPLE002", "ro")]
    text = slack.dms[0][1]
    assert "Auto-approve up to *RO* is on" in text
    assert " until <!date^" in text


def test_a_ticked_box_with_no_end_is_refused_beside_the_end(slack):
    """An auto-approve rule needs an end date (auto_approve_limits). The
    refusal sits beside the end field, and nothing is written."""
    slack.outcome = _written
    _submit(slack, ends="none")
    assert slack.acks == [{"response_action": "errors",
                           "errors": {ag.B_ENDS: auto_approve_limits.required_message(90)}}]
    assert slack.calls == [] and slack.dms == [] and slack.notified == []


def test_an_end_past_a_lowered_limit_is_refused(slack, monkeypatch):
    """The limit can drop between the render and the submit."""
    monkeypatch.setattr(auto_approve_limits, "max_days", lambda: 20)
    _submit(slack, ends="30")
    assert slack.acks == [{"response_action": "errors",
                           "errors": {ag.B_ENDS: auto_approve_limits.too_far_message(20)}}]
    assert slack.calls == []


def test_a_view_built_before_the_end_field_still_refuses_the_box(slack):
    state = _state()
    del state[ag.B_ENDS]
    body = {"user": {"id": ADMIN, "name": "admin"}, "view": {"state": {"values": state}}}
    handlers.handle_grant_submission(slack.ack, body, object())
    assert ag.B_ENDS in slack.acks[0]["errors"] and slack.calls == []


def test_a_plain_grant_can_end_too(slack):
    _submit(slack, auto=False, ends="7")
    assert slack.acks == [None]
    assert {c["auto_approve_tier"] for c in slack.calls} == {None}
    assert all(_close_to(c["expires_at"], 7) for c in slack.calls)
    assert " until <!date^" in slack.dms[0][1]


def test_a_waiver_tier_the_rule_cannot_have_is_said_before_the_end(slack, monkeypatch):
    def refuse(tier, auto):
        raise auto_approve_limits.TierRefused("tier_not_allowed", auto_approve_limits.TIER_MESSAGE)
    monkeypatch.setattr(handlers.grants, "check_waiver_tier", refuse)
    _submit(slack)
    assert slack.acks[0]["errors"] == {ag.B_AUTO: auto_approve_limits.TIER_MESSAGE}
    assert slack.calls == []


def test_grants_without_a_waiver_are_not_affected(slack):
    _submit(slack, auto=False, ends="none")
    assert slack.acks == [None] and len(slack.calls) == 2
    assert {c["expires_at"] for c in slack.calls} == {None}  # no end, as before


def test_an_unticked_box_asks_for_nothing(slack):
    _submit(slack, auto=False, ends="none")
    assert {c["auto_approve_tier"] for c in slack.calls} == {None}
    assert slack.notified == [("U0EXAMPLE002", None)]
    assert "Auto-approve" not in slack.dms[0][1]
    assert "until" not in slack.dms[0][1]


def test_a_waiver_already_in_place_is_named_in_the_summary():
    """The admin's summary line, for a caller that can still write a waiver."""
    text = handlers._grant_waiver_summary("ro", [{"tier": "ro", "written": [], "skipped": [
        {"database": None, "covered_by": "12", "by": "auto-approve #12 (up to RO on every "
         "server they can reach, no expiry)", "reason": "already covered by ..."}]}])
    assert "already existed" in text and "auto-approve #12" in text


def test_a_mixed_outcome_counts_both():
    text = handlers._grant_waiver_summary("ro", [
        {"tier": "ro", "written": [{"id": "1", "database": None}], "skipped": []},
        {"tier": "ro", "written": [], "skipped": [{"database": None, "covered_by": "14",
                                                    "by": "auto-approve #14 (...)",
                                                    "reason": "already covered by ..."}]}])
    assert "1 written, 1 skipped" in text


def test_the_target_change_rebuild_keeps_the_tick(monkeypatch):
    captured = {}
    monkeypatch.setattr(auto_approve_limits, "max_days", lambda: 90)
    monkeypatch.setattr(handlers.grants, "authz", lambda uid: {"super": True, "max_tier": None})
    client = SimpleNamespace(views_update=lambda **kw: captured.update(kw))
    body = {"user": {"id": ADMIN},
            "actions": [{"selected_options": [{"text": {"text": "prod-orders"}, "value": "7"}]}],
            "view": {"id": "V1", "hash": "h", "state": {"values": _state(auto=True)}}}
    handlers.handle_grant_target_changed(lambda: None, body, client)
    el = _block(captured["view"], ag.B_AUTO)["element"]
    assert el.get("initial_options") == el["options"]
    # The picked end survives the rebuild too.
    ends = _block(captured["view"], ag.B_ENDS)["element"]
    assert ends["initial_option"]["value"] == "30"


def test_the_grantee_dm_names_the_end():
    from queryhub import grants
    at = datetime(2026, 12, 3, 9, 30, tzinfo=timezone.utc)
    text = grants.grant_message(["prod-orders"], "ro", None, expires_at=at)
    assert f" until <!date^{int(at.timestamp())}^" in text
    assert "|2026-12-03 09:30 UTC>." in text
    assert "until" not in grants.grant_message(["prod-orders"], "ro", None)
