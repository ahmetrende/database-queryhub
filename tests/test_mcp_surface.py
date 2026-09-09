"""The third door: what a program may do, and what it may not.

QueryHub's governance does not change behind an MCP transport -- same grants,
same classification, same approval, same audit, same masking. These tests pin
the handful of things that are specific to this door, each of which is a way
the door could quietly become wider than the other two.

The tools import no SDK, which is what lets this file run without
`queryhub[mcp]` installed. That is the same property that makes the extra
genuinely optional rather than nominally so, so it is asserted here too.
"""
import inspect

import pytest

from queryhub.mcp_server import caller, policy, server, tools


# --- the door is shut until an operator opens it -----------------------------


def test_installing_the_dependency_does_not_open_the_door():
    """Two separate switches on purpose. Adding a package to a venv is not a
    decision to expose production databases to a program."""
    src = inspect.getsource(policy)
    assert 'ENABLED_KEY = "mcp_enabled"' in src
    assert 'get_setting(ENABLED_KEY, "off")' in src


def test_every_tool_checks_the_switch(monkeypatch):
    monkeypatch.setattr(policy, "enabled", lambda: False)
    for fn, args in ((tools.list_connections, ()),
                     (tools.describe_database, ("c", "d")),
                     (tools.classify_sql, ("c", "select 1")),
                     (tools.submit_query, ("c", None, "select 1")),
                     (tools.query_status, (1,)),
                     (tools.fetch_result, (1,))):
        with pytest.raises(tools.ToolError) as e:
            fn(*args)
        assert e.value.code == "disabled", fn.__name__


# --- the ceiling ------------------------------------------------------------


def test_the_default_ceiling_is_read_only(monkeypatch):
    monkeypatch.setattr(policy.cfg, "get_setting", lambda k, d=None: d)
    assert policy.max_tier() == "ro"
    assert policy.tier_allowed("ro") is True
    assert policy.tier_allowed("rw") is False
    assert policy.tier_allowed("ddl") is False


def test_an_unreadable_ceiling_reads_as_read_only(monkeypatch):
    """The sibling bug fixed on 2026-09-08 was a tier comparison where an
    unrecognised value ranked ABOVE every ceiling, so a typo removed the limit
    instead of the authority. A value nobody can parse must never be the
    permissive one."""
    monkeypatch.setattr(policy.cfg, "get_setting",
                        lambda k, d=None: "READ-ONLY" if k == policy.CEILING_KEY else d)
    assert policy.max_tier() == "ro"


def test_an_unrecognised_requirement_is_refused_not_compared(monkeypatch):
    monkeypatch.setattr(policy, "max_tier", lambda: "ddl")
    assert policy.tier_allowed("superuser") is False
    assert policy.tier_allowed("") is False


def test_widening_needs_no_deploy_and_narrowing_takes_effect_at_once():
    """Read per call, like every other limit here: an operator narrowing this
    during an incident must not wait for a restart."""
    src = inspect.getsource(policy.max_tier)
    assert "cfg.get_setting" in src


# --- the ceiling is asked about the right thing ------------------------------


def test_the_ceiling_is_not_compared_against_the_display_label():
    """`query_safety.required_mode` says in its own docstring that it is NOT an
    authorization input: a BLOCKED statement -- `UPDATE` with no `WHERE`, a
    mixed-tier script, an unparseable fragment -- also reports `ro`. The first
    version of this gate compared against it, and `update t set a = 1` passed
    a read-only ceiling. It asks `analyze()` and reports blockers instead."""
    src = inspect.getsource(tools._classify)
    assert "query_safety.analyze(" in src
    assert "rep.blockers" in src
    assert "required_mode" not in inspect.getsource(tools.submit_query)


def test_a_blocked_statement_is_refused_before_the_ceiling_is_consulted():
    src = inspect.getsource(tools.submit_query)
    assert src.index('ToolError("blocked"') < src.index("policy.tier_allowed(")


# --- identity belongs to the transport ---------------------------------------


def test_no_tool_takes_a_caller_argument():
    """The property that makes the multi-user step small: when a bot fronts
    this for many people, only `caller.acting_principal` changes and no tool
    signature moves. It is also what stops a client naming whoever it likes."""
    for fn in (tools.list_connections, tools.describe_database,
               tools.classify_sql, tools.submit_query, tools.query_status,
               tools.fetch_result):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"user", "user_id", "uid", "principal",
                             "principal_id", "as_user", "on_behalf_of"}, fn.__name__


def test_every_tool_resolves_the_caller_itself():
    for fn in (tools.list_connections, tools.describe_database,
               tools.classify_sql, tools.submit_query, tools.query_status,
               tools.fetch_result):
        assert "_me()" in inspect.getsource(fn), fn.__name__


def test_an_unknown_caller_is_refused_the_way_the_other_doors_refuse():
    """Same gate, deliberately reused: an enabled requester, admins passing
    implicitly. A door that agreed about identity but not about who is
    admitted would widen access by existing."""
    src = inspect.getsource(caller.acting_principal)
    assert "admins.is_admin(pid) or requesters.is_allowed(pid)" in src


def test_a_missing_caller_raises_rather_than_defaulting():
    """A tool running with no caller is a tool running with no grants to
    check."""
    src = inspect.getsource(caller.acting_principal)
    assert 'raise CallerError(\n            "no_caller"' in src
    assert "return None" not in src


def test_an_unexpanded_placeholder_says_so(monkeypatch):
    """Measured against a real editor: Claude Code does NOT expand `${VAR}` in
    an MCP config's `env` block -- it passes the text through. The value then
    arrives as the placeholder, the whitelist refuses it, and the reader is
    told they are not whitelisted, which sends them to look at their QueryHub
    account instead of their config.

    A refusal that names the wrong thing costs more than no refusal.
    """
    import os
    monkeypatch.setitem(os.environ, caller.ENV_VAR, "${QH_MCP_PRINCIPAL}")
    with pytest.raises(caller.CallerError) as e:
        caller.acting_principal()
    assert e.value.code == "unexpanded_placeholder"
    assert "did not expand" in e.value.message


def test_the_client_config_does_not_try_to_expand_a_variable():
    """The `env` block is gone for the same reason: it was actively harmful,
    handing the server a literal instead of an identity. The variable is
    inherited from the environment the client itself runs in."""
    import json
    import pathlib
    cfg = json.loads((pathlib.Path(__file__).resolve().parents[1]
                      / ".mcp.json").read_text(encoding="utf-8"))
    server_cfg = cfg["mcpServers"]["queryhub"]
    assert "env" not in server_cfg
    assert "$" not in json.dumps(server_cfg)


# --- what a program may never ask -------------------------------------------


def test_unmasked_is_not_a_parameter_anywhere():
    """It is refused for anyone who is not a super-admin and re-checked at
    execution, but a door for programs should not be able to ask: the argument
    that reaches the function is the one an assistant can be talked into
    setting."""
    assert "unmasked" not in set(inspect.signature(tools.submit_query).parameters)
    assert "unmasked=False" in inspect.getsource(tools.submit_query)
    assert "confirmed=False" in inspect.getsource(tools.submit_query)


def test_results_are_read_from_the_stored_masked_file():
    """Not re-run. The executor writes the masked CSV, so reading the artefact
    cannot show a program what a person reading the same result would not
    see."""
    src = inspect.getsource(tools.fetch_result)
    assert "csv_file_path" in src
    # Body only: the docstring explains that the EXECUTOR wrote the masked
    # file, so matching the whole source finds the prose and calls it a call.
    body = "\n".join(ln for ln in src.split('"""')[-1].splitlines()
                      if not ln.lstrip().startswith("#"))
    assert "executor" not in body


def test_a_page_is_bounded():
    """A protocol response is read into an assistant's context; an unbounded
    page spends somebody's whole context on one query by accident."""
    assert tools.MAX_ROWS <= 500
    assert "min(int(limit), MAX_ROWS)" in inspect.getsource(tools.fetch_result)


# --- one answer for "not yours" and "not there" ------------------------------


def test_another_persons_request_is_indistinguishable_from_a_missing_one():
    src = inspect.getsource(tools._own)
    assert 'row["requester_slack_id"] != uid' in src
    assert src.count("not_found") == 1


def test_an_ungranted_connection_is_indistinguishable_from_an_unknown_one():
    """Otherwise a caller enumerates real server names by watching which
    refusal comes back."""
    src = inspect.getsource(tools._target_or_refuse)
    assert "effective_grant_for_user" in src
    assert src.count("unknown_connection") == 1


# --- the transport stays thin ------------------------------------------------


def test_the_sdk_is_not_needed_to_import_or_test_this():
    """What makes the extra optional in fact rather than in the README."""
    for mod in (policy, caller, tools):
        assert "import mcp" not in inspect.getsource(mod)
    src = inspect.getsource(server.build)
    assert "from mcp.server.mcpserver import MCPServer" in src


def test_the_adapter_decides_nothing():
    """Everything decidable lives in tools and policy. An authorization check
    that drifts into the transport is one the tests above stop covering."""
    src = inspect.getsource(server)
    for forbidden in ("is_admin", "effective_grant", "tier_allowed",
                      "acting_principal", "required_mode"):
        assert forbidden not in src, forbidden


def test_a_refusal_reaches_the_caller_as_words():
    """An exception escaping to the protocol becomes an opaque internal error,
    and an assistant that gets one retries the same call."""
    src = inspect.getsource(server.build)
    assert "except tools.ToolError" in src
    assert '"message": e.message' in src


def test_the_origin_says_which_door():
    from queryhub import origins
    assert origins.MCP == "mcp"
    assert origins.label("mcp") == "MCP"
    assert "origin=origins.MCP" in inspect.getsource(tools.submit_query)
