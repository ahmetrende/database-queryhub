"""Requester self-cancel — card button wiring (pure blocks, no DB/Slack)."""
from dba_slack_bot.slack_app import notifications as nt


def _req(**kw):
    base = {"id": 321, "requester_slack_id": "UREQ", "target_server_id": None,
            "database_name": "appdb", "query": "UPDATE t SET a=1 WHERE id=2",
            "wants_result": True, "result_format": "csv"}
    base.update(kw)
    return base


def test_pending_card_carries_edit_and_cancel_buttons():
    blocks = nt.requester_card_blocks(
        _req(), status_emoji=":hourglass_flowing_sand:",
        status_text="Submitted", with_cancel=True)
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    edit, cancel = actions[0]["elements"]
    assert edit["action_id"] == nt.ACTION_RESUBMIT      # pre-filled modal
    assert edit["value"] == "321"
    assert "confirm" not in edit                         # editing is safe
    assert cancel["action_id"] == nt.ACTION_CANCEL_REQUEST
    assert cancel["value"] == "321"
    assert cancel["style"] == "danger"
    assert "confirm" in cancel  # accidental-click guard


def test_default_card_has_no_cancel_button():
    blocks = nt.requester_card_blocks(
        _req(), status_emoji=":x:", status_text="Rejected")
    assert not any(
        el.get("action_id") == nt.ACTION_CANCEL_REQUEST
        for b in blocks if b["type"] == "actions"
        for el in b.get("elements", [])
    )


def test_cancel_action_distinct_from_scheduled_cancel():
    # Two different flows (withdraw-pending vs cancel-scheduled) must not
    # collide on one Bolt action registration.
    assert nt.ACTION_CANCEL_REQUEST != nt.ACTION_CANCEL_SCHEDULED


def test_update_requester_card_noop_without_tracking():
    # No stored (channel, ts) → must not touch Slack at all.
    class _Boom:
        def __getattr__(self, name):
            raise AssertionError("Slack client must not be called")
    nt.update_requester_card(
        _Boom(), _req(requester_dm_channel_id=None,
                      requester_dm_message_ts=None),
        status_emoji=":x:", status_text="whatever")