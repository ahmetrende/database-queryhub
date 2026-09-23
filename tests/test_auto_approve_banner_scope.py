"""The /sql modal's auto-approve badge and window button are scoped.

A waiver covers a connection, or one database on it -- not the person. The
banner asked "does this person hold any waiver at all" and acted on the answer
everywhere: the badge told someone covered on one server that every query would
dispatch immediately, and the request button disappeared, so a person covered
on one server could never ask for a window on another from Slack. The burst DM
had the same gate and stayed silent for the same people.
"""
import json
from types import SimpleNamespace

import pytest

from queryhub import auto_approve
from queryhub.slack_app import handlers, modal, ro_window

UID = "U0EXAMPLE001"
ALIASES = {53: "prod-ledger", 60: "prod-orders"}


def _w(tid, dbn, tier="ro", team_id=None, team_name=None):
    return {"id": 1, "slack_user_id": UID, "max_tier": tier, "target_server_id": tid,
            "database_name": dbn, "starts_at": None, "expires_at": None,
            "reason": None, "granted_by": None, "team_id": team_id, "team_name": team_name}


@pytest.fixture
def env(monkeypatch):
    state = {"rows": [], "burst": None, "reach": True, "dms": [], "boom": False}

    def active_grants(pid, at=None):
        if state["boom"]:
            raise RuntimeError("control DB down")
        return state["rows"]
    monkeypatch.setattr(auto_approve, "active_grants", active_grants)
    monkeypatch.setattr(auto_approve, "_team_waiver_applies",
                        lambda pid, tid, dbn: state["reach"])
    monkeypatch.setattr(modal, "_recent_ro_burst", lambda pid: state["burst"])
    monkeypatch.setattr(modal.targets, "get",
                        lambda tid: SimpleNamespace(alias=ALIASES.get(tid, "x")))
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda client, pid, text=None, blocks=None: state["dms"].append(blocks))
    return state


def _actions(blocks):
    out = []
    for b in blocks:
        if b.get("accessory"):
            out.append(b["accessory"])
        out.extend(b.get("elements", []) if b.get("type") == "actions" else [])
    return out


def _text(blocks):
    return json.dumps(blocks, ensure_ascii=False)


# --- the button --------------------------------------------------------------

def test_a_waiver_elsewhere_does_not_hide_the_request_button(env):
    env["rows"] = [_w(53, "ledger")]
    blocks = modal._auto_approve_banner(UID)
    assert any(a.get("action_id") == ro_window.ACTION_OPEN for a in _actions(blocks))


def test_a_burst_on_an_uncovered_database_gets_the_prominent_nudge(env):
    env["rows"] = [_w(53, "ledger")]
    env["burst"] = {"count": 4, "target_server_id": 60, "database_name": "orders"}
    blocks = modal._auto_approve_banner(UID)
    [btn] = [a for a in _actions(blocks) if a.get("action_id") == ro_window.ACTION_OPEN]
    assert btn["text"]["text"] == "Request window"
    assert json.loads(btn["value"]) == {"t": 60, "d": "orders"}


def test_a_burst_on_a_covered_database_stands_the_nudge_down(env):
    """Those reads already skip review: the prominent nudge is noise there,
    but the modest button stays for everywhere else."""
    env["rows"] = [_w(53, "ledger")]
    env["burst"] = {"count": 4, "target_server_id": 53, "database_name": "ledger"}
    blocks = modal._auto_approve_banner(UID)
    assert "Running a lot of reads?" not in _text(blocks)
    [btn] = [a for a in _actions(blocks) if a.get("action_id") == ro_window.ACTION_OPEN]
    assert btn["text"]["text"] == "Request"


def test_a_failing_waiver_read_never_blocks_the_modal(env):
    env["boom"] = True
    blocks = modal._auto_approve_banner(UID)
    assert any(a.get("action_id") == ro_window.ACTION_OPEN for a in _actions(blocks))
    assert "Auto-approve active" not in _text(blocks)


# --- the badge ---------------------------------------------------------------

def test_the_badge_names_where_the_waiver_applies(env):
    env["rows"] = [_w(53, "ledger")]
    text = _text(modal._auto_approve_banner(UID))
    assert "Auto-approve active* on `prod-ledger` / `ledger`" in text
    assert "anything else still waits for approval" in text


def test_the_badge_names_the_team_a_waiver_comes_from(env):
    env["rows"] = [_w(53, "ledger", team_id=7, team_name="Team Alpha")]
    assert "via Team Alpha" in _text(modal._auto_approve_banner(UID))


def test_a_team_waiver_that_does_not_reach_the_member_is_not_promised(env):
    env["reach"] = False
    env["rows"] = [_w(53, "ledger", team_id=7, team_name="Team Alpha")]
    assert "Auto-approve active" not in _text(modal._auto_approve_banner(UID))


def test_a_fleet_wide_waiver_says_so(env):
    env["rows"] = [_w(None, None)]
    assert "on every connection" in _text(modal._auto_approve_banner(UID))


def test_a_long_list_is_capped(env):
    env["rows"] = [_w(53, f"db{i}") for i in range(5)]
    assert "and 2 more" in _text(modal._auto_approve_banner(UID))


# --- the burst DM ------------------------------------------------------------

def test_the_burst_dm_fires_for_someone_covered_only_elsewhere(env):
    env["rows"] = [_w(53, "ledger")]
    env["burst"] = {"count": 3, "target_server_id": 60, "database_name": "orders"}
    handlers._maybe_dm_ro_burst(None, UID, "ro")
    assert len(env["dms"]) == 1


def test_the_burst_dm_stays_silent_where_reads_already_skip_review(env):
    env["rows"] = [_w(60, "orders")]
    env["burst"] = {"count": 3, "target_server_id": 60, "database_name": "orders"}
    handlers._maybe_dm_ro_burst(None, UID, "ro")
    assert env["dms"] == []


def test_a_wider_waiver_hides_the_narrower_ones_it_contains(env):
    """Three servers listed for someone covered everywhere hid the one fact
    that mattered behind "and 2 more"."""
    env["rows"] = [_w(53, None), _w(60, None), _w(61, None), _w(62, None), _w(None, None)]
    text = _text(modal._auto_approve_banner(UID))
    assert "on every connection (up to *RO*" in text
    assert "prod-ledger" not in text and "more" not in text


def test_a_higher_tier_on_a_narrower_scope_is_kept(env):
    env["rows"] = [_w(None, None, tier="ro"), _w(53, "ledger", tier="rw")]
    text = _text(modal._auto_approve_banner(UID))
    assert "every connection" in text and "`prod-ledger` / `ledger` (up to *RW*" in text
