"""The access-request card shows no SQL recipe.

The card used to give an admin SQL to run before pressing Approve. Approve
writes the grant itself, so the recipe was extra work. It also went stale once:
after the pod cutover it named tables that were empty, and the paste failed.

The requester's own attempted query stays on the card. That is information for
the admin, not a recipe.
"""
from queryhub.slack_app import access


def _request(**over):
    row = {
        "id": 17,
        "requester_slack_id": "U0REQUESTER",
        "database_name": None,
        "reason": "need one read-only look",
        "attempted_query": "SELECT 1",
    }
    row.update(over)
    return row


def _texts(blocks: list[dict]) -> str:
    """Every visible string on the card, joined."""
    out: list[str] = []
    for b in blocks:
        if isinstance(b.get("text"), dict):
            out.append(b["text"].get("text", ""))
        for f in b.get("fields", []):
            out.append(f.get("text", ""))
        for e in b.get("elements", []):
            if e.get("type") == "mrkdwn":
                out.append(e.get("text", ""))
    return "\n".join(out)


def test_the_card_has_no_sql_recipe():
    text = _texts(access.admin_dm_blocks(_request(), None))
    for needle in ("INSERT INTO", "TEAM_NAME", "ON CONFLICT",
                   "team_admin_templates"):
        assert needle not in text


def test_the_only_code_fence_is_the_attempted_query():
    blocks = access.admin_dm_blocks(_request(), None)
    # One fence pair, around the requester's own query.
    assert _texts(blocks).count("```") == 2
    assert "SELECT 1" in _texts(blocks)


def test_no_fence_when_nothing_was_attempted():
    blocks = access.admin_dm_blocks(_request(attempted_query=None), None)
    assert "```" not in _texts(blocks)


def test_the_card_says_what_approve_does():
    text = _texts(access.admin_dm_blocks(_request(), None))
    assert "Approve" in text
    assert "per-user grant" in text
    assert "default RO" in text


def test_the_buttons_stay_last():
    blocks = access.admin_dm_blocks(_request(), None)
    last = blocks[-1]
    assert last["type"] == "actions"
    ids = [e["action_id"] for e in last["elements"]]
    assert ids == [access.ACTION_APPROVE, access.ACTION_REJECT]
    assert "confirm" in last["elements"][0]


def test_the_resolved_card_swaps_the_buttons_for_the_status():
    blocks = access.resolved_admin_dm_blocks(_request(), None, "Approved")
    assert all(b["type"] != "actions" for b in blocks)
    assert blocks[-1]["type"] == "context"
    assert blocks[-1]["elements"][0]["text"] == "Approved"
    assert "INSERT INTO" not in _texts(blocks)
