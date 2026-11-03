"""The catalog and the tool functions agree on every argument.

`catalog.call` passes only the arguments that the client sent. So an argument
that the catalog marks optional must have a default in the function, and an
argument that the catalog requires must not. Otherwise a call without that
argument is a TypeError, which the door reports as an internal error.

`database` is required. A program that leaves it out gets `bad_arguments` and
never reaches the default database of the connection.
"""
import inspect
from types import SimpleNamespace

import pytest

from queryhub import db
from queryhub.mcp_server import catalog, policy, tools

ME = "U0EXAMPLE001"
PG = SimpleNamespace(id=7, alias="example-pg", engine="postgres")


@pytest.mark.parametrize("tool", catalog.TOOLS, ids=lambda t: t.name)
def test_the_function_takes_the_arguments_that_the_catalog_names(tool):
    params = inspect.signature(getattr(tools, tool.name)).parameters
    assert set(params) == set(tool.properties)


@pytest.mark.parametrize("tool", catalog.TOOLS, ids=lambda t: t.name)
def test_only_the_required_arguments_have_no_default(tool):
    params = inspect.signature(getattr(tools, tool.name)).parameters
    no_default = {n for n, p in params.items() if p.default is inspect.Parameter.empty}
    assert no_default == set(tool.required)


@pytest.fixture
def submit(monkeypatch):
    """A submit that goes to a person, with no database behind it."""
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)
    seen = {}

    def validate(*a, **k):
        seen.update(k)
        return object()
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools, "_target_or_refuse", lambda uid, c: PG)
    monkeypatch.setattr(tools.caller, "display_name", lambda uid: "Ex")
    monkeypatch.setattr(tools.core_submit, "validate_submission", validate)
    monkeypatch.setattr(tools.core_submit, "create_request", lambda prep: SimpleNamespace(
        auto_approved=False, row={"id": 41, "status": "pending"}))
    monkeypatch.setattr(tools.core_submit, "dispatch_and_notify", lambda *a, **k: None)
    monkeypatch.setattr(tools.core_submit, "Rejection", type("R", (), {}))
    monkeypatch.setattr(policy, "max_tier", lambda: "ro")
    return seen


@pytest.mark.parametrize("database", [None, "absent"])
def test_submit_query_without_a_database_is_refused(submit, database):
    args = {"connection": "example-pg", "sql": "SELECT 1"}
    if database is None:
        args["database"] = None
    with pytest.raises(tools.ToolError) as e:
        catalog.call("submit_query", args)
    assert e.value.code == "bad_arguments" and "'database'" in e.value.message
    assert submit == {}, "nothing was submitted"


def test_the_catalog_requires_a_database_for_submit_query():
    submit = next(t for t in catalog.listing() if t["name"] == "submit_query")
    assert submit["inputSchema"]["required"] == ["connection", "sql", "database"]


def test_submit_query_through_the_catalog_with_a_database(submit):
    out = catalog.call("submit_query", {"connection": "example-pg", "sql": "SELECT 1",
                                        "database": "ledger"})
    assert out["requestId"] == 41 and submit["database_name"] == "ledger"
