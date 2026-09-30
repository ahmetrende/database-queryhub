"""/sql grant can carry auto-approve for read-only queries.

One optional checkbox on the grant modal. Ticked, every grants.grant() call
the submission makes also writes the RO waiver, in the grant's own
transaction; the grantee's DM and the admin's summary say so, and the summary
names a waiver that was skipped because one already covers it.

No Slack, no DB: the handler is called with a fake ack and client.
"""
from types import SimpleNamespace

import pytest

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


def _state(auto=True, tier="rw", users=("U0EXAMPLE002",), targets=(7, 8)):
    return {
        ag.B_USER: {ag.A_USER: {"selected_users": list(users)}},
        ag.B_TARGET: {ag.A_TARGET: {"selected_options": [{"value": str(t)} for t in targets]}},
        ag.B_TIER: {ag.A_TIER: {"selected_option": {"value": tier}}},
        ag.B_DBS: {ag.A_DBS: {"selected_options": []}},
        ag.B_REASON: {ag.A_REASON: {"value": "onboarding"}},
        ag.B_AUTO: {ag.A_AUTO: {"selected_options": [{"value": "ro"}] if auto else []}},
    }


def test_the_state_reader():
    assert ag.auto_approve_tier(_state(auto=True)) == "ro"
    assert ag.auto_approve_tier(_state(auto=False)) is None
    assert ag.auto_approve_tier({}) is None                  # a view built before the box


# --- the submission ----------------------------------------------------------

@pytest.fixture
def slack(monkeypatch):
    st = SimpleNamespace(calls=[], dms=[], notified=[], acks=[], outcome=None)

    def grant(**kw):
        st.calls.append(kw)
        out = st.outcome(kw) if st.outcome else None
        return {"mode": kw["mode"], "databases": None, "whitelisted_now": False,
                "auto_approve": out}

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


def test_a_ticked_box_asks_every_grant_for_the_ro_waiver(slack):
    slack.outcome = _written
    _submit(slack)
    assert slack.acks == [None]                              # the modal closed
    assert [(c["grantee_id"], c["target_id"], c["auto_approve_tier"]) for c in slack.calls] == [
        ("U0EXAMPLE002", 7, "ro"), ("U0EXAMPLE002", 8, "ro")]
    assert slack.notified == [("U0EXAMPLE002", "ro")]
    [(to, text)] = slack.dms
    assert to == ADMIN and "Auto-approve up to *RO* is on for these." in text


def test_an_unticked_box_asks_for_nothing(slack):
    _submit(slack, auto=False)
    assert {c["auto_approve_tier"] for c in slack.calls} == {None}
    assert slack.notified == [("U0EXAMPLE002", None)]
    assert "Auto-approve" not in slack.dms[0][1]


def test_a_waiver_already_in_place_is_named_in_the_summary(slack):
    slack.outcome = lambda kw: {"tier": "ro", "written": [], "skipped": [
        {"database": None, "covered_by": "12", "by": "auto-approve #12 (up to RO on every "
         "server they can reach, no expiry)", "reason": "already covered by ..."}]}
    _submit(slack)
    text = slack.dms[0][1]
    assert "already in place" in text and "auto-approve #12" in text
    # Nothing new was written, so the grantee is not told it as news.
    assert slack.notified == [("U0EXAMPLE002", None)]


def test_a_mixed_outcome_counts_both(slack):
    slack.outcome = lambda kw: (_written(kw) if kw["target_id"] == 7 else {
        "tier": "ro", "written": [], "skipped": [{"database": None, "covered_by": "14",
                                                   "by": "auto-approve #14 (...)",
                                                   "reason": "already covered by ..."}]})
    _submit(slack)
    assert "1 written, 1 skipped" in slack.dms[0][1]
    assert slack.notified == [("U0EXAMPLE002", "ro")]


def test_the_target_change_rebuild_keeps_the_tick(monkeypatch):
    captured = {}
    monkeypatch.setattr(handlers.grants, "authz", lambda uid: {"super": True, "max_tier": None})
    client = SimpleNamespace(views_update=lambda **kw: captured.update(kw))
    body = {"user": {"id": ADMIN},
            "actions": [{"selected_options": [{"text": {"text": "prod-orders"}, "value": "7"}]}],
            "view": {"id": "V1", "hash": "h", "state": {"values": _state(auto=True)}}}
    handlers.handle_grant_target_changed(lambda: None, body, client)
    el = _block(captured["view"], ag.B_AUTO)["element"]
    assert el.get("initial_options") == el["options"]
