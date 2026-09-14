"""Admin grant/revoke — pure helpers + modal structure (no DB/Slack)."""
from queryhub import grants
from queryhub.slack_app import admin_grant as ag
from queryhub.slack_app import handlers


def test_db_options_read_targets_from_private_metadata(monkeypatch):
    # The cascade-quirk fix: the DB picker must derive its target ids from
    # private_metadata, not the (unreliable) live state.
    monkeypatch.setattr(handlers.grants, "authz",
                        lambda uid: {"super": True, "max_tier": None})
    monkeypatch.setattr(handlers.schema_catalog, "list_snapshot_databases",
                        lambda tid: {15: ["crm", "kyc"], 9: ["config_service"]}.get(tid, []))
    captured = {}
    body = {"user": {"id": "UADMIN"},
            "view": {"private_metadata": '{"targets": [15, 9]}',
                     "state": {"values": {}}}}   # state deliberately empty
    handlers.handle_grant_db_options(lambda p: captured.update(p), {"value": ""}, body)
    vals = sorted(o["value"] for o in captured["options"])
    assert vals == ["config_service", "crm", "kyc"]   # union across both targets


# --- grants.allowed_tiers ---------------------------------------------------

def test_allowed_tiers_super_admin_all():
    assert grants.allowed_tiers({"super": True, "max_tier": None}) == ["ro", "rw", "ddl"]


def test_allowed_tiers_scoped_ro():
    assert grants.allowed_tiers({"super": False, "max_tier": "ro"}) == ["ro"]


def test_allowed_tiers_scoped_rw():
    assert grants.allowed_tiers({"super": False, "max_tier": "rw"}) == ["ro", "rw"]


# --- grant_modal ------------------------------------------------------------

def _tier_values(modal):
    blk = next(b for b in modal["blocks"] if b.get("block_id") == ag.B_TIER)
    return [o["value"] for o in blk["element"]["options"]]


def test_grant_modal_tier_options_match_allowed():
    m = ag.grant_modal(allowed_tiers=["ro"])
    assert _tier_values(m) == ["ro"]
    # Several people at once: access is handed out to a group as often as to a
    # person, and the modal used to make that N passes with everything else
    # retyped.
    assert m["blocks"][0]["element"]["type"] == "multi_users_select"
    assert m["submit"]["text"] == "Grant access"


def test_grant_modal_target_is_multi_select_with_dispatch():
    # Targets: multi-select AND dispatch_action, so a change updates
    # private_metadata (which the DB picker reads — the cascade-quirk fix).
    m = ag.grant_modal(allowed_tiers=["ro", "rw", "ddl"])
    tgt = next(b for b in m["blocks"] if b.get("block_id") == ag.B_TARGET)
    assert tgt["element"]["type"] == "multi_external_select"
    assert tgt["dispatch_action"] is True
    assert '"targets": []' in m["private_metadata"]


def test_grant_modal_preserves_inputs_on_rebuild():
    # A re-render (after a target change) must carry the user's entries.
    m = ag.grant_modal(
        allowed_tiers=["ro", "rw", "ddl"], grantees=["U123", "U456"],
        target_initial_options=[{"text": {"type": "plain_text", "text": "t"},
                                 "value": "15"}],
        tier="rw", reason="why", target_ids=[15])
    import json as _json
    assert _json.loads(m["private_metadata"])["targets"] == [15]
    user_b = next(b for b in m["blocks"] if b.get("block_id") == ag.B_USER)
    assert user_b["element"]["initial_users"] == ["U123", "U456"]
    tier_b = next(b for b in m["blocks"] if b.get("block_id") == ag.B_TIER)
    assert tier_b["element"]["initial_option"]["value"] == "rw"
    reason_b = next(b for b in m["blocks"] if b.get("block_id") == ag.B_REASON)
    assert reason_b["element"]["initial_value"] == "why"
    tgt_b = next(b for b in m["blocks"] if b.get("block_id") == ag.B_TARGET)
    assert tgt_b["element"]["initial_options"][0]["value"] == "15"


def test_grant_modal_databases_is_multi_select_optional():
    # DBs are picked, not typed from memory — a multi-select fed by the
    # A_DBS options handler. Optional (empty = all databases).
    m = ag.grant_modal(allowed_tiers=["ro"])
    dbs = next(b for b in m["blocks"] if b.get("block_id") == ag.B_DBS)
    assert dbs["element"]["type"] == "multi_external_select"
    assert dbs["optional"] is True


def test_grant_modal_full_tiers():
    m = ag.grant_modal(allowed_tiers=["ro", "rw", "ddl"])
    assert _tier_values(m) == ["ro", "rw", "ddl"]
    # initial option is the lowest tier
    tier_blk = next(b for b in m["blocks"] if b.get("block_id") == ag.B_TIER)
    assert tier_blk["element"]["initial_option"]["value"] == "ro"


# --- revoke_modal -----------------------------------------------------------

def test_revoke_modal_picker_is_external_not_workspace_wide():
    # The picker must be an external_select (fed by list_granted_users),
    # NOT a users_select — revoke should list only users with grants, not
    # the whole Slack workspace.
    m = ag.revoke_modal()
    assert "submit" not in m                       # no form submit
    picker = m["blocks"][0]
    assert picker["type"] == "actions"
    assert picker["elements"][0]["type"] == "external_select"


def test_revoke_user_option_label():
    opt = ag.revoke_user_option(
        {"slack_user_id": "U9", "name": "Jane Doe", "n_grants": 2})
    assert opt["value"] == "U9"
    assert "Jane Doe" in opt["text"]["text"] and "2 grants" in opt["text"]["text"]
    # no name → fall back to id; singular
    opt2 = ag.revoke_user_option(
        {"slack_user_id": "U8", "name": None, "n_grants": 1})
    assert "U8" in opt2["text"]["text"] and "1 grant" in opt2["text"]["text"]


def test_revoke_modal_lists_grants_with_buttons():
    m = ag.revoke_modal(selected_user="U9", grants=[
        {"target_server_id": 3, "alias": "prod-alpha", "mode": "rw",
         "allowed_databases": None},
        {"target_server_id": 4, "alias": "prod-beta", "mode": "ro",
         "allowed_databases": ["ledger"]},
    ])
    btns = [b["accessory"] for b in m["blocks"] if b.get("accessory")]
    assert len(btns) == 2
    assert all(b["action_id"] == ag.ACTION_REVOKE_ONE for b in btns)
    assert btns[0]["value"] == "U9:3"
    assert all("confirm" in b for b in btns)       # accidental-click guard


def test_revoke_modal_user_with_no_grants():
    m = ag.revoke_modal(selected_user="U9", grants=[])
    text = " ".join(b.get("text", {}).get("text", "") for b in m["blocks"]
                     if b["type"] == "section")
    assert "no active per-user grants" in text


# --- several people at once, and the person you are already talking to -------

def test_the_grant_modal_takes_more_than_one_person():
    """Access is handed to a GROUP as often as to a person — a new joiner and
    their two teammates, an on-call rota. The modal used to be one pass each,
    with the target, tier, databases and reason retyped every time."""
    import queryhub.slack_app.admin_grant as ag
    m = ag.grant_modal(allowed_tiers=["ro"])
    el = next(b for b in m["blocks"] if b.get("block_id") == ag.B_USER)["element"]
    assert el["type"] == "multi_users_select"
    assert "initial_users" not in el          # nothing preselected by default


def test_the_modal_does_not_try_to_read_a_dm_it_cannot_see():
    """A DM between two PEOPLE is a conversation the bot is not in, and a bot
    token may only read conversations it belongs to — `conversations.info`
    answers `channel_not_found`, measured against the live workspace. Opening
    the modal must not spend an API call discovering that on every /sql grant.

    The shape that CAN carry the person is a message shortcut, whose payload
    has `message.user`. Until that exists, the field opens empty."""
    from queryhub.slack_app import subcommands as sc
    import inspect
    src = inspect.getsource(sc._handle_grant)
    assert "conversations_info" not in src
    assert "grantees=" not in src
