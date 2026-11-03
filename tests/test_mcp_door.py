"""The MCP tools for any caller: identity, arguments, the approver card.

What these tests pin:
1. Identity is never an argument. A tool takes no person, and the door binds
   the person with `caller.acting_as` only.
2. `catalog.call` checks the arguments: names, required ones and types.
3. `/mcp` is outside `/api`, so no web dependency judges an MCP call.
4. The approver sees that Claude wrote a request.
5. `/sql claude` is in the help list for every requester.

The HTTP door itself is in `test_mcp_idp_door.py`.
Nothing here may reach a database.
"""
from __future__ import annotations

import pytest

from queryhub import db
from queryhub.mcp_server import caller, catalog, tools
from queryhub.web import routes_mcp

ME = "U0EXAMPLE001"
OTHER = "U0EXAMPLE002"


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)


def test_the_router_has_no_web_dependency():
    """`/mcp` is outside `/api`, so no web dependency runs."""
    assert routes_mcp.router.prefix == "/mcp"
    assert all(not r.dependant.dependencies for r in routes_mcp.router.routes)


# ---------------------------------------------------------------------------
# identity is never an argument
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["uid", "user", "user_id", "principal", "slack_user_id", "caller"])
def test_a_tool_cannot_be_told_who_to_act_as(monkeypatch, key):
    monkeypatch.setattr(tools, "_me", lambda: ME)
    with pytest.raises(tools.ToolError) as e:
        catalog.call("list_connections", {key: OTHER})
    assert e.value.code == "bad_arguments"


def test_no_catalog_tool_names_a_person():
    for tool in catalog.TOOLS:
        assert not set(tool.properties) & {"uid", "user", "user_id", "principal",
                                           "slack_user_id", "caller", "unmasked"}


def test_the_door_binds_the_person_with_acting_as_and_nothing_else():
    import inspect
    src = inspect.getsource(routes_mcp._call_tool)
    assert "caller.acting_as(uid)" in src
    assert "QH_MCP_PRINCIPAL" not in src


def test_acting_as_refuses_an_empty_person():
    with pytest.raises(caller.CallerError):
        with caller.acting_as(""):
            pass


def test_acting_as_unbinds_the_person_on_exit():
    with caller.acting_as(ME):
        assert caller._BOUND.get() == ME
    assert caller._BOUND.get() is None
    with pytest.raises(RuntimeError), caller.acting_as(ME):
        raise RuntimeError("the tool failed")
    assert caller._BOUND.get() is None, "a failed call unbinds the person too"


def test_the_bound_person_still_has_to_be_whitelisted(monkeypatch):
    monkeypatch.setattr(caller.admins, "is_admin", lambda p: False)
    monkeypatch.setattr(caller.requesters, "is_allowed", lambda p: False)
    with caller.acting_as(ME):
        with pytest.raises(caller.CallerError) as e:
            caller.acting_principal()
    assert e.value.code == "not_whitelisted"


@pytest.mark.parametrize("args,code", [
    ({"nope": 1}, "bad_arguments"),
    ({"connection": "c"}, "bad_arguments"),               # database missing
    ({"connection": "c", "database": 7}, "bad_arguments"),
    ({"connection": "c", "database": ""}, "bad_arguments"),    # blank is missing
    ({"connection": " ", "database": "d"}, "bad_arguments"),
])
def test_arguments_are_checked(args, code):
    with pytest.raises(tools.ToolError) as e:
        catalog.call("describe_database", args)
    assert e.value.code == code


def test_a_bool_is_not_an_integer():
    with pytest.raises(tools.ToolError) as e:
        catalog.call("query_status", {"request_id": True})
    assert e.value.code == "bad_arguments"


def test_an_unknown_tool_is_refused():
    with pytest.raises(tools.ToolError) as e:
        catalog.call("drop_everything", {})
    assert e.value.code == "unknown_tool"


def test_the_command_is_in_the_help_list():
    from queryhub.slack_app import subcommands
    row = next(r for r in subcommands._SUBCOMMANDS if r[0] == "claude")
    assert row[2] is False, "every requester may connect Claude"


# ---------------------------------------------------------------------------
# the approver card
# ---------------------------------------------------------------------------

def _card(origin, sql, risk=None):
    from queryhub import targets
    from queryhub.slack_app import notifications
    target = targets.TargetServer(
        id=7, alias="example-pg", host="pg.example.test", port=5432,
        default_database="ledger", username="rw", enabled=True, notes=None,
        engine="postgres", engine_config={})
    request = {"id": 41, "requester_slack_id": ME, "database_name": "ledger", "query": sql,
               "origin": origin, "risk_summary": risk, "justification": "fix the typo",
               "result_format": "csv", "wants_result": True}
    blocks = notifications._request_blocks(request, target, with_actions=False)
    return "\n".join(str(b) for b in blocks)


def test_the_approver_sees_that_claude_wrote_a_write():
    text = _card("mcp", "UPDATE t SET a = 1 WHERE id = 2")
    assert "Claude wrote this statement" in text and "fix the typo" in text
    assert "Pre-flight: no plan" in text


def test_the_approver_sees_the_plan_when_there_is_one():
    text = _card("mcp", "UPDATE t SET a = 1 WHERE id = 2", risk="Plan: 1 row")
    assert "Claude wrote this statement" in text and "Plan: 1 row" in text
    assert "Pre-flight: no plan" not in text


def test_a_slack_or_web_card_is_unchanged():
    for origin in ("slack", "web", "idp"):
        text = _card(origin, "UPDATE t SET a = 1 WHERE id = 2")
        assert "Claude wrote" not in text and "Pre-flight: no plan" not in text
