"""AI and MCP on/off per connection (migration 217), and the question a host
or port change asks about the stored credentials (P1.3).

Nothing here reaches a database. Every read is faked.
"""
from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from queryhub import ai, db_scope, schema_tools, targets
from queryhub.mcp_server import policy, tools
from queryhub.web import admin as web_admin
from queryhub.web import routes_admin as ra
from queryhub.web import routes_data, routes_queries

ME = "U0EXAMPLE001"
SUPER = {"sub": "U000EXAMPLE", "name": "Super Admin"}
DBA = {"sub": "U000SCOPED1", "name": "Scoped Dba"}


def _target(tid: int = 7, alias: str = "example-pg", *, ai_allowed: bool = True,
            mcp_allowed: bool = True) -> targets.TargetServer:
    return targets.TargetServer(
        id=tid, alias=alias, host="pg.example.test", port=5432,
        default_database="ledger", username="reader", enabled=True, notes=None,
        engine="postgres", engine_config={}, ai_allowed=ai_allowed,
        mcp_allowed=mcp_allowed)


def _grant(mode: str = "ro") -> dict:
    return {"mode": mode, "allowed_databases": None, "source": "team"}


# ---------------------------------------------------------------------------
# targets: the flags load, and an old row reads as allowed
# ---------------------------------------------------------------------------

def test_a_row_without_the_columns_reads_as_allowed():
    t = targets._row_to_target({
        "id": 1, "alias": "a", "host": "h", "port": 5432,
        "default_database": "d", "username": "u", "enabled": True})
    assert t.ai_allowed is True and t.mcp_allowed is True


def test_a_row_with_the_columns_false_reads_as_closed():
    t = targets._row_to_target({
        "id": 1, "alias": "a", "host": "h", "port": 5432,
        "default_database": "d", "username": "u", "enabled": True,
        "ai_allowed": False, "mcp_allowed": False})
    assert t.ai_allowed is False and t.mcp_allowed is False


def test_the_admin_select_reads_both_columns():
    assert "ai_allowed" in targets._ADMIN_COLS
    assert "mcp_allowed" in targets._ADMIN_COLS


# ---------------------------------------------------------------------------
# AI: ai._granted
# ---------------------------------------------------------------------------

@pytest.fixture
def ai_grant(monkeypatch):
    st = {"target": _target()}
    monkeypatch.setattr(ai.schema_tools, "granted_database",
                        lambda uid, conn, database, named=None: (st["target"], _grant()))
    monkeypatch.setattr(ai.admins, "is_admin", lambda uid: False)
    return st


def test_ai_refuses_a_connection_with_ai_off(ai_grant):
    ai_grant["target"] = _target(ai_allowed=False)
    with pytest.raises(ai.AIError) as e:
        ai._granted(ME, "example-pg", "ledger")
    assert e.value.status == 403 and e.value.code == "forbidden"
    assert e.value.message == ("The AI assistant is off for this connection. "
                               "An admin can turn it on.")


def test_ai_passes_a_connection_with_ai_on(ai_grant):
    assert ai._granted(ME, "example-pg", "ledger").alias == "example-pg"


def test_the_ai_refusal_comes_before_a_table_name_is_read(ai_grant, monkeypatch):
    """`complete` reads table names only after `_granted` returns."""
    ai_grant["target"] = _target(ai_allowed=False)
    monkeypatch.setattr(schema_tools, "table_names",
                        lambda *a, **k: pytest.fail("a table name was read"))
    with pytest.raises(ai.AIError):
        ai._granted(ME, "example-pg", "ledger")


def test_a_web_request_is_ai_assisted_only_where_the_target_allows_it():
    src = inspect.getsource(routes_queries)
    assert 'getattr(t, "ai_allowed", True)' in src


# ---------------------------------------------------------------------------
# MCP: tools._granted, list_connections, _own
# ---------------------------------------------------------------------------

def test_mcp_refuses_a_connection_closed_to_mcp(monkeypatch):
    monkeypatch.setattr(tools.schema_tools, "granted_target",
                        lambda uid, conn, named=None: (_target(mcp_allowed=False), _grant()))
    with pytest.raises(tools.ToolError) as e:
        tools._granted(ME, "example-pg")
    assert e.value.code == "mcp_closed"
    assert e.value.message == ("This connection is closed to MCP clients. "
                               "An admin can open it.")


def test_mcp_passes_an_open_connection(monkeypatch):
    monkeypatch.setattr(tools.schema_tools, "granted_target",
                        lambda uid, conn, named=None: (_target(), _grant()))
    t, _g = tools._granted(ME, "example-pg")
    assert t.alias == "example-pg"


def test_list_connections_leaves_out_a_connection_closed_to_mcp(monkeypatch):
    fleet = [_target(7, "open-pg"), _target(8, "closed-pg", mcp_allowed=False)]
    asked: list = []
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools.targets, "list_enabled", lambda: list(fleet))
    monkeypatch.setattr(tools.teams, "effective_grants_for_user",
                        lambda uid, ids: asked.extend(ids) or {i: _grant() for i in ids})
    monkeypatch.setattr(db_scope, "catalog_databases_map",
                        lambda ids: {i: ["ledger"] for i in ids})
    monkeypatch.setattr(db_scope, "visible_databases", lambda t, g, names: ["ledger"])
    monkeypatch.setattr(tools.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(policy, "max_tier", lambda: "rw")
    monkeypatch.setattr(policy, "result_mode", lambda: "inline")
    out = tools.list_connections()
    assert [c["connection"] for c in out["connections"]] == ["open-pg"]
    assert 8 not in asked


def _request_row(**over) -> dict:
    row = {"id": 5, "status": "completed", "requester_slack_id": ME,
           "target_server_id": 7, "database_name": "ledger", "query": "SELECT 1",
           "row_count": 1, "truncated": False, "required_tier": "ro",
           "error_message": None, "csv_file_path": None, "created_at": None,
           "completed_at": None}
    row.update(over)
    return row


def test_own_refuses_a_request_on_a_connection_closed_to_mcp(monkeypatch):
    monkeypatch.setattr(tools.db, "fetch_one",
                        lambda sql, params=None: _request_row(mcp_allowed=False))
    with pytest.raises(tools.ToolError) as e:
        tools._own(5, ME)
    assert e.value.code == "mcp_closed"


def test_own_returns_a_request_on_an_open_connection(monkeypatch):
    seen: list = []
    monkeypatch.setattr(tools.db, "fetch_one",
                        lambda sql, params=None: seen.append(sql) or _request_row(mcp_allowed=True))
    row = tools._own(5, ME)
    assert row["id"] == 5 and "mcp_allowed" not in row
    # The flag comes from the request's own target.
    assert "requests.target_server_id" in seen[0]


def test_own_still_reads_a_row_without_the_flag(monkeypatch):
    """A fake or an old row with no flag is allowed: today's behaviour."""
    monkeypatch.setattr(tools.db, "fetch_one", lambda sql, params=None: _request_row())
    assert tools._own(5, ME)["id"] == 5


# ---------------------------------------------------------------------------
# Admin: the two switches and the list entry
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, returning=None):
        self.calls: list = []
        self.returning = returning

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.returning


class FakeTxn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


def _row(**over) -> dict:
    row = {
        "id": 42, "alias": "prod-beta", "host": "db.example.internal",
        "port": 5432, "default_database": "ledger", "enabled": True,
        "notes": None, "engine": "postgres", "secrets_provider": "local",
        "ai_allowed": True, "mcp_allowed": True,
        "credentials": {
            "ro": {"username": "reader_ro", "configured": True, "placeholder": False},
            "rw": {"username": None, "configured": False, "placeholder": False},
            "ddl": {"username": None, "configured": False, "placeholder": False},
        },
    }
    row.update(over)
    return row


@pytest.fixture
def wire(monkeypatch):
    state = {"row": _row(), "cur": FakeCursor({"id": 42}), "audit": [],
             "super": True}
    monkeypatch.setattr(web_admin.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(web_admin.admins, "is_super_admin", lambda uid: state["super"])
    monkeypatch.setattr(ra.admins, "is_super_admin", lambda uid: state["super"])
    monkeypatch.setattr(ra.targets, "by_alias",
                        lambda alias: (type("T", (), {"id": state["row"]["id"]})()
                                       if alias == state["row"]["alias"] else None))
    monkeypatch.setattr(ra.targets, "admin_row", lambda tid: state["row"])
    monkeypatch.setattr(ra.targets, "alias_holders", lambda cur=None: {
        state["row"]["alias"].lower(): {"id": state["row"]["id"],
                                        "alias": state["row"]["alias"],
                                        "engine": state["row"]["engine"],
                                        "enabled": state["row"]["enabled"]}})
    monkeypatch.setattr(ra.db, "transaction", lambda: FakeTxn(state["cur"]))
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        state["audit"].append((action, details)))
    return state


def _route(path_end: str) -> set:
    return {m for r in ra.router.routes
            if getattr(r, "path", "").endswith(path_end)
            for m in getattr(r, "methods", set())}


def test_the_two_switch_routes_exist():
    assert "PUT" in _route("/connections/{conn}/ai-allowed")
    assert "PUT" in _route("/connections/{conn}/mcp-allowed")


@pytest.mark.parametrize("fn, column, action, key", [
    (ra.admin_set_ai_allowed, "ai_allowed", "connection_ai_allowed_set", "aiAllowed"),
    (ra.admin_set_mcp_allowed, "mcp_allowed", "connection_mcp_allowed_set", "mcpAllowed"),
])
def test_a_super_admin_switches_a_flag_and_it_is_audited(wire, fn, column, action, key):
    out = fn("prod-beta", ra.AllowedIn(allowed=False), claims=SUPER)
    assert out == {"connectionId": "prod-beta", key: False, "changed": True}
    [(sql, params)] = wire["cur"].calls
    assert f"SET {column} = %s" in sql and params == (False, 42, False)
    assert wire["audit"] == [(action, {"target": "prod-beta", "targetId": 42,
                                       "old": True, "new": False})]


@pytest.mark.parametrize("fn", [ra.admin_set_ai_allowed, ra.admin_set_mcp_allowed])
def test_no_change_writes_no_audit_row(wire, fn):
    wire["cur"].returning = None
    out = fn("prod-beta", ra.AllowedIn(allowed=True), claims=SUPER)
    assert out["changed"] is False and wire["audit"] == []


@pytest.mark.parametrize("fn", [ra.admin_set_ai_allowed, ra.admin_set_mcp_allowed])
def test_only_a_super_admin_switches_a_flag(wire, fn):
    wire["super"] = False
    with pytest.raises(HTTPException) as e:
        fn("prod-beta", ra.AllowedIn(allowed=False), claims=DBA)
    assert e.value.status_code == 403
    assert wire["cur"].calls == [] and wire["audit"] == []


def test_the_switches_use_the_super_admin_only_checks():
    for fn in (ra.admin_set_ai_allowed, ra.admin_set_mcp_allowed):
        src = inspect.getsource(fn)
        assert 'admin.require_admin(claims, "access")' in src
        assert "_require_super(uid" in src


def test_the_admin_entry_carries_both_flags():
    e = ra._connection_entry(_row(ai_allowed=False), [])
    assert e["aiAllowed"] is False and e["mcpAllowed"] is True
    keys = list(e)
    assert keys.index("aiAllowed") == keys.index("superAdminOnly") + 1
    assert keys.index("mcpAllowed") == keys.index("aiAllowed") + 1


def test_an_admin_entry_without_the_columns_reads_as_allowed():
    row = _row()
    del row["ai_allowed"], row["mcp_allowed"]
    e = ra._connection_entry(row, [])
    assert e["aiAllowed"] is True and e["mcpAllowed"] is True


def test_the_user_entry_carries_ai_allowed():
    assert '"aiAllowed": bool(getattr(t, "ai_allowed", True))' in inspect.getsource(routes_data)


# ---------------------------------------------------------------------------
# P1.3: a host or port change asks about the stored credentials
# ---------------------------------------------------------------------------

@pytest.fixture
def writes(wire, monkeypatch):
    st = {"update": [], "creds": []}
    monkeypatch.setattr(ra.targets, "update_in",
                        lambda cur, tid, changes: st["update"].append(changes) or list(changes))
    monkeypatch.setattr(ra.targets, "set_credentials_in",
                        lambda cur, tid, mode, username, password:
                        st["creds"].append((mode, username, password)))
    monkeypatch.setattr(routes_data, "_catalog_databases", lambda tid: ["ledger"])
    return st


def test_a_host_change_without_confirmation_asks(wire, writes):
    with pytest.raises(HTTPException) as e:
        ra.admin_update_connection("prod-beta",
                                   ra.ConnectionPatch(host="db2.example.internal"),
                                   claims=SUPER)
    assert e.value.status_code == 409
    assert e.value.detail["code"] == "confirmation_required"
    assert e.value.detail["reasons"] == [
        "The login and password stay the same. "
        "Confirm that they work on the new host."]
    assert writes["update"] == [] and writes["creds"] == [] and wire["audit"] == []


def test_a_port_only_change_also_asks(wire, writes):
    with pytest.raises(HTTPException) as e:
        ra.admin_update_connection("prod-beta", ra.ConnectionPatch(port=5433),
                                   claims=SUPER)
    assert e.value.status_code == 409
    assert e.value.detail["code"] == "confirmation_required"


def test_a_confirmed_host_change_saves_and_keeps_the_credentials(wire, writes):
    ra.admin_update_connection(
        "prod-beta",
        ra.ConnectionPatch(host="db2.example.internal", port=5433,
                           confirmCredentials=True),
        claims=SUPER)
    assert writes["update"] == [{"host": "db2.example.internal", "port": 5433}]
    assert writes["creds"] == []
    action, details = wire["audit"][-1]
    assert action == "connection_updated"
    assert details["host_before"] == "db.example.internal"
    assert details["host_after"] == "db2.example.internal"
    assert details["port_before"] == 5432 and details["port_after"] == 5433
    assert details["credentials"] == []
    assert "password" not in repr(details).lower()


def test_a_host_change_that_sends_a_new_password_does_not_ask(wire, writes):
    ra.admin_update_connection(
        "prod-beta",
        ra.ConnectionPatch(host="db2.example.internal",
                           credentials={"ro": {"username": "reader_ro",
                                               "password": "new-one"}}),
        claims=SUPER)
    assert writes["update"] == [{"host": "db2.example.internal"}]
    assert writes["creds"] == [("ro", "reader_ro", "new-one")]


def test_a_blank_password_box_is_not_a_new_credential(wire, writes):
    """The form sends the user name with an empty password on every save."""
    with pytest.raises(HTTPException) as e:
        ra.admin_update_connection(
            "prod-beta",
            ra.ConnectionPatch(host="db2.example.internal",
                               credentials={"ro": {"username": "reader_ro",
                                                   "password": ""}}),
            claims=SUPER)
    assert e.value.status_code == 409


def test_a_target_with_no_stored_credential_does_not_ask(wire, writes):
    for tier in wire["row"]["credentials"].values():
        tier["configured"] = False
    ra.admin_update_connection("prod-beta",
                               ra.ConnectionPatch(host="db2.example.internal"),
                               claims=SUPER)
    assert writes["update"] == [{"host": "db2.example.internal"}]


def test_the_same_host_and_port_do_not_ask(wire, writes):
    ra.admin_update_connection(
        "prod-beta",
        ra.ConnectionPatch(host="db.example.internal", port=5432, notes="moved"),
        claims=SUPER)
    assert writes["update"] == [{"notes": "moved"}]
    _action, details = wire["audit"][-1]
    assert "host_before" not in details
