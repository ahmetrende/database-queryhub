"""The access-request card tells the truth when the request names no connection.

With the request-access list off, every typed server is stored as free text with
no connection. `access_requests.decide` then flips the status and skips the
grant (`no_target`). The card used to say "Approve gives the requester a
per-user grant", and the confirm dialog said "Approve and grant?". An admin who
read that believed the access existed. It did not.

The card and the dialog now say what Approve does for each case:

* a request that names a connection: Approve writes the grant;
* a request that names none: Approve only marks it approved, and the admin
  matches the typed server to a connection and grants access in the Grants
  screen.
"""
from types import SimpleNamespace

from queryhub.slack_app import access

CONNECTION = SimpleNamespace(id=7, alias="prod-main", host="db.example.internal")


def _request(**over):
    row = {
        "id": 19,
        "requester_slack_id": "U0REQUESTER",
        "database_name": "payments",
        "reason": "[server: 123] [requested tier: RO] need a look",
        "attempted_query": "web-endpoint-request:123|RO|payments",
    }
    row.update(over)
    return row


def _section_texts(blocks):
    return "\n".join(b["text"]["text"] for b in blocks
                     if isinstance(b.get("text"), dict))


def _approve_button(blocks):
    [actions] = [b for b in blocks if b.get("type") == "actions"]
    [approve] = [e for e in actions["elements"] if e["text"]["text"] == "Approve"]
    return approve


# ---------------------------------------------------------------------------
# no connection
# ---------------------------------------------------------------------------

def test_the_card_says_approve_grants_nothing_when_no_connection_is_named():
    blocks = access.admin_dm_blocks(_request(), None, requested_server="123")
    text = _section_texts(blocks)
    assert "names no connection" in text
    assert "It grants no access" in text
    assert "Grants screen" in text
    assert "gives the requester a per-user grant" not in text


def test_the_confirm_dialog_does_not_promise_a_grant_when_no_connection_is_named():
    approve = _approve_button(access.admin_dm_blocks(_request(), None,
                                                     requested_server="123"))
    confirm = approve["confirm"]
    assert confirm["title"]["text"] == "Approve without a grant?"
    assert "creates no grant" in confirm["text"]["text"]
    assert "automatically" not in confirm["text"]["text"]


def test_a_removed_connection_gets_the_same_honest_card():
    """The row had a connection that no longer exists. Nothing can be granted."""
    blocks = access.admin_dm_blocks(_request(), None)
    assert "It grants no access" in _section_texts(blocks)


# ---------------------------------------------------------------------------
# a connection
# ---------------------------------------------------------------------------

def test_the_card_still_says_approve_grants_when_a_connection_is_named():
    blocks = access.admin_dm_blocks(_request(attempted_query=None), CONNECTION)
    text = _section_texts(blocks)
    assert "gives the requester a per-user grant" in text
    assert "names no connection" not in text


def test_the_confirm_dialog_still_says_grant_when_a_connection_is_named():
    approve = _approve_button(access.admin_dm_blocks(_request(attempted_query=None),
                                                     CONNECTION))
    assert approve["confirm"]["title"]["text"] == "Approve and grant?"
    assert "creates the per-user grant" in approve["confirm"]["text"]["text"]


# ---------------------------------------------------------------------------
# what happens after the click says the same thing
# ---------------------------------------------------------------------------

def test_the_status_line_after_approval_says_no_grant_was_written():
    status, dm = access.approval_texts(
        dict(_request(), auto_grant={"applied": False, "reason": "no_target"}),
        "<@UADMIN>")
    assert "Auto-grant skipped: the request names no connection" in status
    assert "A DBA will finalize your access shortly." in dm
