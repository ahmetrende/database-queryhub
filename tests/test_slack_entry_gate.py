"""The Slack entry gate: bot_config slack_own_ui_super_admin_only.

With the switch on, only a super-admin or a person on the allowed list can use
/sql. Everyone else gets a notice with the portal link. Approval buttons and
result messages are not part of the gate.
"""
import pytest

from queryhub import admins
from queryhub import config as cfg
from queryhub.slack_app import entry_gate, handlers

SUPER = "U0SUPER0001"
LISTED = "U0LISTED001"
OTHER = "U0OTHER0001"
PORTAL = "https://portal.example.test/queryhub"


@pytest.fixture
def gate(monkeypatch):
    """bot_config with the gate keys that the test sets."""
    keys = {entry_gate.SWITCH: "on", entry_gate.ALLOWED_USERS: f" {LISTED} ,",
            "web_portal_url": PORTAL}
    defaults = cfg.get_setting          # the conftest stub

    def get_setting(key, default=None):
        return keys[key] if key in keys else defaults(key, default)

    monkeypatch.setattr(cfg, "get_setting", get_setting)
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: uid == SUPER)
    return keys


# --- who passes --------------------------------------------------------------

def test_with_the_switch_off_everyone_passes(gate):
    gate[entry_gate.SWITCH] = "off"
    assert entry_gate.is_open(OTHER)


@pytest.mark.parametrize("uid, expected", [
    (SUPER, True), (LISTED, True), (OTHER, False), ("", False), (None, False)])
def test_with_the_switch_on_only_super_admins_and_the_list_pass(gate, uid, expected):
    assert entry_gate.is_open(uid) is expected


def test_a_read_fault_refuses(gate, monkeypatch):
    def boom(_uid):
        raise RuntimeError("metadata database is down")
    monkeypatch.setattr(admins, "is_super_admin", boom)
    assert entry_gate.is_open(OTHER) is False


def test_the_notice_links_the_portal(gate):
    assert f"<{PORTAL}|QueryHub in the portal>" in entry_gate.notice_text()


def test_without_a_portal_address_the_notice_says_whom_to_ask(gate):
    gate["web_portal_url"] = "javascript:alert(1)"
    text = entry_gate.notice_text()
    assert "<" not in text and "Ask the DBA team" in text


# --- the handlers ------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.acks, self.responses, self.views = [], [], []

    def ack(self, payload=None):
        self.acks.append(payload)

    def respond(self, payload):
        self.responses.append(payload)

    def views_open(self, **kwargs):
        self.views.append(kwargs)


@pytest.mark.parametrize("text", ["", "history", "help", "grant"])
def test_slash_sql_refuses_a_closed_person_with_the_notice(gate, monkeypatch, text):
    monkeypatch.setattr(handlers.subcommands, "dispatch",
                        lambda *a, **k: pytest.fail("ran a subcommand"))
    rec = _Recorder()
    handlers.handle_slash_sql(rec.ack, {"user_id": OTHER, "text": text,
                                        "trigger_id": "t"}, rec, rec.respond)
    assert rec.acks == [None]
    assert rec.responses == [entry_gate.ephemeral()]
    assert rec.views == []


def test_slash_sql_lets_a_listed_person_through(gate, monkeypatch):
    ran = []
    monkeypatch.setattr(handlers.requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(handlers.profile_sync, "maybe_backfill_user_profile",
                        lambda *a: None)
    monkeypatch.setattr(handlers.subcommands, "dispatch",
                        lambda text, uid, *a: ran.append((text, uid)))
    rec = _Recorder()
    handlers.handle_slash_sql(rec.ack, {"user_id": LISTED, "text": "history"},
                              rec, rec.respond)
    assert ran == [("history", LISTED)]


@pytest.mark.parametrize("handler", [
    handlers.handle_view_submission,
    handlers.handle_batch_submission,
    handlers.handle_import_submission,
])
def test_a_modal_submit_of_a_closed_person_shows_the_notice(gate, monkeypatch, handler):
    monkeypatch.setattr(handlers, "_log_submission_failure", lambda *a, **k: None)
    rec = _Recorder()
    handler(rec.ack, {"user": {"id": OTHER}, "view": {}}, rec)
    assert rec.acks == [{"response_action": "update",
                         "view": entry_gate.notice_view()}]


def test_submit_again_shows_the_notice_instead_of_the_modal(gate, monkeypatch):
    monkeypatch.setattr(handlers.db, "fetch_one", lambda *a, **k: {
        "id": 7, "requester_slack_id": OTHER, "target_server_id": 1,
        "database_name": "d", "query": "SELECT 1", "justification": "",
        "result_format": "csv", "wants_result": True, "status": "completed"})
    monkeypatch.setattr(handlers.modal, "build_modal",
                        lambda **k: pytest.fail("opened the /sql modal"))
    rec = _Recorder()
    handlers.handle_resubmit(rec.ack, {"user": {"id": OTHER}, "trigger_id": "t",
                                       "actions": [{"value": "7"}]}, rec)
    assert rec.views == [{"trigger_id": "t", "view": entry_gate.notice_view()}]
