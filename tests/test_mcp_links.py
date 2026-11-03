"""The link to the request, in what Claude reads back.

Every request comes back with `requestUrl`. It opens the request as a tab in the
person's QueryHub workspace in the portal: the SQL, the state, and the result
when it ran. The approvers get the request in Slack and in their queue, so the
person needs no forwarding step. The link carries no secret and no identity. It
is absent when the portal address is unknown, so Claude never shows a broken
link.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from queryhub import db
from queryhub.mcp_server import catalog, policy, tools
from queryhub.web import routes_queries

ME = "U0EXAMPLE001"
PG = SimpleNamespace(id=7, alias="example-pg", engine="postgres")


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)


PORTAL = "https://portal.example.test/queryhub"
LINK = PORTAL + "?request="


def _address(monkeypatch, value):
    monkeypatch.setattr(policy.cfg, "get_setting",
                        lambda key, default=None: value if key == "mcp_portal_url" else default)


def test_a_link_opens_the_request_in_the_portal(monkeypatch):
    _address(monkeypatch, PORTAL + "/")
    assert policy.portal_base() == PORTAL
    assert policy.request_url(7) == LINK + "7"


@pytest.mark.parametrize("value", ["", "   ", None, "/queryhub", "javascript:alert(1)",
                                   "https://u:p@portal.example.test/queryhub",
                                   "https://portal.example.test/queryhub#x",
                                   "https://portal.example.test/queryhub?tab=welcome"])
def test_no_usable_portal_address_gives_no_link(monkeypatch, value):
    _address(monkeypatch, value)
    assert policy.portal_base() is None and policy.request_url(1) is None


@pytest.fixture
def waiting(monkeypatch):
    """A submit that goes to a person: validated, created, not auto-approved."""
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools, "_target_or_refuse", lambda uid, c: PG)
    monkeypatch.setattr(tools.caller, "display_name", lambda uid: "Ex")
    monkeypatch.setattr(tools.core_submit, "validate_submission", lambda *a, **k: object())
    monkeypatch.setattr(tools.core_submit, "create_request", lambda prep: SimpleNamespace(
        auto_approved=False, row={"id": 41, "status": "pending"}))
    monkeypatch.setattr(tools.core_submit, "dispatch_and_notify", lambda *a, **k: None)
    monkeypatch.setattr(tools.core_submit, "Rejection", type("R", (), {}))
    monkeypatch.setattr(policy, "max_tier", lambda: "ro")


def test_a_waiting_request_carries_the_request_link(waiting, monkeypatch):
    _address(monkeypatch, PORTAL)
    out = tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")
    assert out["decision"] == "needs_approval" and out["requestId"] == 41
    assert out["requestUrl"] == LINK + "41"


def test_a_submit_tells_the_approvers_with_the_slack_client_of_the_web(waiting, monkeypatch):
    """The door runs in the web process. Without the web's Slack client the
    approvers get no DM for a request from Claude."""
    client, sent = object(), []
    monkeypatch.setattr(routes_queries, "_bot_client", lambda: client)
    monkeypatch.setattr(tools.core_submit, "dispatch_and_notify",
                        lambda c, prep, outcome, **k: sent.append((c, k)))
    tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")
    assert sent == [(client, {"dm_requester": False})]


def test_a_waiting_request_without_an_address_has_no_link(waiting, monkeypatch):
    _address(monkeypatch, "")
    out = tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")
    assert out["decision"] == "needs_approval" and "requestUrl" not in out


def _status(monkeypatch, status):
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools, "_own", lambda rid, uid: {
        "id": rid, "status": status, "row_count": None, "error_message": None,
        "truncated": False, "required_tier": "ro", "query": "SELECT 1",
        "csv_file_path": None, "created_at": datetime.now(timezone.utc),
        "completed_at": None})


@pytest.mark.parametrize("status", ["pending", "approved", "completed", "failed", "rejected"])
def test_every_status_carries_the_link(monkeypatch, status):
    """The person opens the request in the workspace at any state: to watch it
    wait, to read the result, or to read why it failed."""
    _address(monkeypatch, PORTAL)
    _status(monkeypatch, status)
    assert tools.query_status(41)["requestUrl"] == LINK + "41"


def test_an_auto_approved_submit_carries_the_link_too(waiting, monkeypatch):
    _address(monkeypatch, PORTAL)
    monkeypatch.setattr(tools.core_submit, "create_request", lambda prep: SimpleNamespace(
        auto_approved=True, row={"id": 42, "status": "approved"}))
    monkeypatch.setattr(tools, "_await_result",
                        lambda rid, seconds, started: {"status": "completed"})
    out = tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")
    assert out["decision"] == "auto_approved"
    assert out["requestUrl"] == LINK + "42"


def test_the_descriptions_tell_claude_to_show_the_link():
    text = json.dumps(catalog.listing())
    assert "`requestUrl`" in text and "approvalUrl" not in text
