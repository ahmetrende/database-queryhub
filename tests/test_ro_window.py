"""ro_window request modal + CTA (pure block-builders, no Slack/DB)."""
from queryhub.slack_app import ro_window


class _T:
    """Minimal TargetServer stand-in for the picker options."""
    def __init__(self, id, alias):
        self.id = id
        self.alias = alias


def test_window_options_and_validation():
    mins = [m for m, _ in ro_window.WINDOW_OPTIONS]
    assert mins == [60, 180, 480]  # 1h / 3h / 8h
    assert all(ro_window.is_valid_window(m) for m in mins)
    assert not ro_window.is_valid_window(90)
    assert not ro_window.is_valid_window(0)


def test_request_modal_has_target_window_reason():
    m = ro_window.request_modal(
        user_targets=[_T(1, "a"), _T(2, "b")], default_minutes=60)
    blocks = {b.get("block_id") for b in m["blocks"] if b.get("block_id")}
    assert {ro_window.B_TARGET, ro_window.B_WINDOW, ro_window.B_REASON} <= blocks
    assert m["callback_id"] == ro_window.MODAL_CALLBACK
    # window defaults to 1 hour (60) and offers all three options
    wsel = next(b for b in m["blocks"] if b.get("block_id") == ro_window.B_WINDOW)["element"]
    assert wsel["initial_option"]["value"] == "60"
    assert [o["value"] for o in wsel["options"]] == ["60", "180", "480"]


def test_request_modal_preselects_target_when_in_list():
    m = ro_window.request_modal(
        user_targets=[_T(5, "x"), _T(30, "staking")],
        default_minutes=180, preselect_target_id=30)
    tsel = next(b for b in m["blocks"] if b.get("block_id") == ro_window.B_TARGET)["element"]
    assert tsel["initial_option"]["value"] == "30"
    # a bad default_minutes falls back to 60, not an unlisted value
    wsel = next(b for b in m["blocks"] if b.get("block_id") == ro_window.B_WINDOW)["element"]
    assert wsel["initial_option"]["value"] == "180"


def test_request_modal_no_preselect_when_target_absent():
    m = ro_window.request_modal(
        user_targets=[_T(5, "x")], preselect_target_id=999)  # not in list
    tsel = next(b for b in m["blocks"] if b.get("block_id") == ro_window.B_TARGET)["element"]
    assert "initial_option" not in tsel


def test_request_modal_bad_default_minutes_falls_back_to_60():
    m = ro_window.request_modal(user_targets=[_T(1, "a")], default_minutes=90)
    wsel = next(b for b in m["blocks"] if b.get("block_id") == ro_window.B_WINDOW)["element"]
    assert wsel["initial_option"]["value"] == "60"


def test_cta_blocks_open_the_request_action_cold():
    # Section + accessory button (reflows on narrow screens; short label).
    block = ro_window.request_cta_blocks()[0]
    assert block["type"] == "section"
    btn = block["accessory"]
    assert btn["action_id"] == ro_window.ACTION_OPEN
    assert btn["value"] == "{}"  # cold open: no preselect target
    assert len(btn["text"]["text"]) <= 12  # short label, won't truncate
