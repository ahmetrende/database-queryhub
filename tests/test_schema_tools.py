"""The schema tools that the MCP door and the AI assistant share (M13).

`schema_tools.table_detail` gives one table in full: the catalog part from
the bot DB and the live part from the target. These tests pin the name
lookup, the shape of the answer, what happens when the target does not
answer, and the audit row.
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from queryhub import audit, db, pii, schema_tools, table_facts, targets
from queryhub.mcp_server import caller, catalog, tools

ME = "U0EXAMPLE001"
PG = SimpleNamespace(id=7, alias="srv-alpha", engine="postgres", host="h", port=5432)
AT = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def _row(schema="public", table="orders", **kw):
    row = {"id": 11, "schema_name": schema, "table_name": table, "relkind": "table",
           "row_estimate": 1200, "total_bytes": 81920, "partition_count": None,
           "partition_key": None, "snapshot_at": AT,
           "indexes": json.dumps([{"name": "orders_pkey",
                                   "def": "CREATE UNIQUE INDEX orders_pkey ON public.orders USING btree (id)"}]),
           "foreign_keys": [{"name": "orders_user_fk",
                             "def": "FOREIGN KEY (user_id) REFERENCES users(id)"}]}
    row.update(kw)
    return row


COLUMNS = [
    {"column_name": "id", "data_type": "bigint", "not_null": True,
     "default_expr": None, "is_pk": True, "in_index": True},
    {"column_name": "email", "data_type": "text", "not_null": False,
     "default_expr": None, "is_pk": False, "in_index": False},
    {"column_name": "state", "data_type": "text", "not_null": True,
     "default_expr": "'new'::text", "is_pk": False, "in_index": False},
]


@pytest.fixture
def bot_db(monkeypatch):
    """A bot DB with one catalogued table. `st["rows"]` is what the table
    lookup returns, `st["reads"]` the lookups made."""
    st = {"rows": [_row()], "reads": [], "audit": []}

    def fetch_all(sql, params=None):
        if "FROM schema_tables" in sql:
            st["reads"].append(params)
            return list(st["rows"])
        if "FROM schema_columns" in sql:
            return list(COLUMNS)
        raise AssertionError(f"unexpected read: {sql}")

    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(pii, "column_pii_map", lambda names: {names.index("email"): "email"})
    monkeypatch.setattr(audit, "log", lambda *a, **k: st["audit"].append(a))
    return st


# --- the name ---------------------------------------------------------------


@pytest.mark.parametrize("name, want", [
    ("orders", ["orders"]),
    ("public.orders", ["public", "orders"]),
    ('"Order Lines"', ["Order Lines"]),
    ("[dbo].[Order Lines]", ["dbo", "Order Lines"]),
    ("`shop`.`t`", ["shop", "t"]),
    (' public . "a""b" ', ["public", 'a"b']),
])
def test_name_parts_reads_quoted_and_bare_names(name, want):
    assert schema_tools.name_parts(name) == want


@pytest.mark.parametrize("name", ["", "a..b", '"open', "a b", "a.", ".a"])
def test_name_parts_refuses_a_broken_name(name):
    assert schema_tools.name_parts(name) is None


def test_a_three_part_name_must_name_this_database(bot_db):
    """`otherdb.public.orders` gets the same answer as a missing table, and the
    catalog is not read: no name reaches a database outside the grant."""
    with pytest.raises(schema_tools.ToolError) as e:
        schema_tools.find_table(PG, "shop", "otherdb.public.orders")
    assert e.value.code == "no_match"
    assert bot_db["reads"] == []
    assert schema_tools.find_table(PG, "shop", "SHOP.public.orders")["table_name"] == "orders"
    assert bot_db["reads"][-1] == (7, "shop", "orders", "public", "public")


def test_a_long_name_is_refused_without_a_read(bot_db):
    with pytest.raises(schema_tools.ToolError):
        schema_tools.find_table(PG, "shop", "x" * (schema_tools.MAX_NAME_CHARS + 1))
    assert bot_db["reads"] == []


def test_an_exact_case_match_wins_over_a_folded_one(bot_db):
    bot_db["rows"] = [_row(table="Orders", id=1), _row(table="orders", id=2)]
    assert schema_tools.find_table(PG, "shop", "Orders")["id"] == 1
    assert schema_tools.find_table(PG, "shop", "orders")["id"] == 2


def test_two_schemas_with_the_name_ask_for_the_schema(bot_db):
    bot_db["rows"] = [_row(schema="public"), _row(schema="archive")]
    with pytest.raises(schema_tools.ToolError) as e:
        schema_tools.find_table(PG, "shop", "orders")
    assert e.value.code == "ambiguous"
    assert "`public.orders`" in e.value.message and "`archive.orders`" in e.value.message


def test_a_missing_table_names_the_next_call(bot_db):
    bot_db["rows"] = []
    with pytest.raises(schema_tools.ToolError) as e:
        schema_tools.find_table(PG, "shop", "nothing", named=catalog.named)
    assert e.value.code == "no_match"
    assert "`queryhub_describe_database`" in e.value.message


def test_database_dot_table_falls_back_to_the_table(bot_db, monkeypatch):
    """ClickHouse and Athena write `database.table`. The first read takes
    `shop` as a schema and finds nothing, the second reads the table alone."""
    answers = [[], [_row(schema="shop", table="events")]]
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None: (
        bot_db["reads"].append(params) or answers.pop(0)))
    assert schema_tools.find_table(PG, "shop", "shop.events")["table_name"] == "events"
    assert bot_db["reads"][-1] == (7, "shop", "events", None, None)


# --- the answer ---------------------------------------------------------------


def _live(monkeypatch, result=None, error=None):
    calls = []

    def live(t, database, trow):
        calls.append((t.alias, database, trow["schema_name"], trow["table_name"]))
        if error is not None:
            raise error
        return dict(result or {})
    monkeypatch.setattr(schema_tools, "_live_part", live)
    return calls


def test_the_answer_joins_the_catalog_and_the_live_part(bot_db, monkeypatch):
    calls = _live(monkeypatch, {"createScript": "CREATE TABLE public.orders (...);",
                                "triggers": [], "columnStats": [], "activity": None,
                                "indexUse": []})
    out = schema_tools.table_detail(ME, "Ex", PG, "shop", "orders", via="mcp")
    assert calls == [("srv-alpha", "shop", "public", "orders")]
    assert (out["schema"], out["table"], out["kind"]) == ("public", "orders", "table")
    assert out["rowEstimate"] == 1200 and out["totalBytes"] == 81920
    assert out["columns"][0] == {"name": "id", "type": "bigint", "nullable": False,
                                 "default": None, "pk": True, "indexed": True}
    assert out["columns"][1]["masked"] is True
    assert "masked" not in out["columns"][2]
    assert out["columns"][2]["default"] == "'new'::text"
    assert out["indexes"][0]["name"] == "orders_pkey"
    assert out["foreignKeys"][0]["name"] == "orders_user_fk"
    assert out["catalogAt"] == AT.isoformat()
    assert out["createScript"].startswith("CREATE TABLE")
    assert "liveError" not in out


def test_each_call_writes_one_audit_row_with_the_door(bot_db, monkeypatch):
    _live(monkeypatch, {"createScript": None})
    schema_tools.table_detail(ME, "Ex", PG, "shop", "orders", via="ai")
    assert bot_db["audit"] == [(None, ME, "Ex", "table_detail_read",
                                {"target_id": 7, "database": "shop",
                                 "object": "public.orders", "via": "ai",
                                 "live": True})]


def test_a_failed_live_read_keeps_the_catalog_part_and_hides_the_driver_text(
        bot_db, monkeypatch):
    _live(monkeypatch, error=RuntimeError("could not connect to db-7.internal as reader"))
    out = schema_tools.table_detail(ME, "Ex", PG, "shop", "orders", via="mcp")
    assert out["columns"] and out["indexes"]
    assert out["liveError"].startswith("The server did not answer.")
    assert "internal" not in json.dumps(out) and "reader" not in json.dumps(out)
    assert bot_db["audit"][0][4]["live"] is False


def test_a_target_with_no_read_login_says_so(bot_db, monkeypatch):
    monkeypatch.setattr(targets, "get_credentials",
                        lambda tid, mode: ("reader", targets.SENTINEL_PASSWORD))
    out = schema_tools.table_detail(ME, "Ex", PG, "shop", "orders", via="mcp")
    assert "no read login" in out["liveError"]
    assert "createScript" not in out


def test_a_long_script_is_cut_and_says_so(bot_db, monkeypatch):
    _live(monkeypatch, {"createScript": "x" * (schema_tools.MAX_SCRIPT_CHARS + 50)})
    out = schema_tools.table_detail(ME, "Ex", PG, "shop", "orders", via="mcp")
    assert len(out["createScript"]) == schema_tools.MAX_SCRIPT_CHARS
    assert out["createScriptTruncated"] is True


def test_the_postgres_read_is_read_only_with_a_statement_timeout(bot_db, monkeypatch):
    """One connection with the read login, a READ ONLY transaction before the
    first read, and the exact catalog names (not the name the caller wrote)."""
    seen = {"sql": [], "kwargs": None, "ddl": None, "facts": None}

    class Cur:
        def execute(self, sql, params=None):
            seen["sql"].append(sql)

    @contextlib.contextmanager
    def connect(**kwargs):
        seen["kwargs"] = kwargs
        conn = SimpleNamespace(cursor=lambda: contextlib.nullcontext(Cur()))
        yield conn

    def ddl(cur, schema, table):
        assert seen["sql"] == ["SET TRANSACTION READ ONLY"]
        seen["ddl"] = (schema, table)
        return ("table", "CREATE TABLE public.orders ();", True)

    def facts(cur, schema, table):
        seen["facts"] = (schema, table)
        return {"triggers": [{"name": "t1", "def": "CREATE TRIGGER t1 ...", "enabled": True}],
                "columnStats": [], "activity": None, "indexUse": []}

    monkeypatch.setattr(schema_tools.psycopg, "connect", connect)
    monkeypatch.setattr(schema_tools.pg_ddl, "object_ddl", ddl)
    monkeypatch.setattr(table_facts, "pg_facts", facts)
    monkeypatch.setattr(targets, "get_credentials", lambda tid, mode: ("reader", "pw"))
    monkeypatch.setattr(schema_tools.cfg, "target_ssl_kwargs", lambda host: {})
    out = schema_tools.table_detail(ME, "Ex", PG, "shop", "ORDERS", via="mcp")
    assert seen["kwargs"]["user"] == "reader" and seen["kwargs"]["dbname"] == "shop"
    assert "statement_timeout" in seen["kwargs"]["options"]
    assert seen["ddl"] == ("public", "orders") and seen["facts"] == ("public", "orders")
    assert out["triggers"][0]["name"] == "t1"


def test_the_mssql_read_returns_triggers_and_statistics(bot_db, monkeypatch):
    from queryhub import mssql_exec
    ms = SimpleNamespace(id=8, alias="srv-beta", engine="mssql", host="h", port=1433)
    monkeypatch.setattr(targets, "get_credentials", lambda tid, mode: ("reader", "pw"))
    monkeypatch.setattr(mssql_exec, "object_ddl",
                        lambda *a, **k: ("table", "CREATE TABLE [dbo].[orders] ();", True))
    monkeypatch.setattr(mssql_exec, "table_facts", lambda *a, **k: {
        "triggers": [{"name": "trg", "def": "CREATE TRIGGER trg ...", "enabled": False}],
        "rows": 1200, "stats": [{"name": "PK_orders", "columns": ["id"]}]})
    out = schema_tools.table_detail(ME, "Ex", ms, "shop", "orders", via="mcp")
    assert out["triggers"][0]["enabled"] is False
    assert out["statistics"][0]["columns"] == ["id"] and out["liveRows"] == 1200


# --- the MCP tool -------------------------------------------------------------


@pytest.fixture
def door(monkeypatch):
    st = {"calls": []}
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools, "_granted", lambda uid, c: (PG, {"mode": "ro",
                                                              "allowed_databases": {"shop"}}))
    monkeypatch.setattr(caller, "display_name", lambda uid: "Ex")
    monkeypatch.setattr(schema_tools, "table_detail",
                        lambda *a, **k: st["calls"].append((a, k)) or {"ok": True})
    return st


def test_the_mcp_tool_passes_the_door_and_the_portal_names(door):
    assert catalog.call("describe_table", {"connection": "srv-alpha", "database": "shop",
                                           "table": "orders"}) == {"ok": True}
    (args, kwargs), = door["calls"]
    assert args == (ME, "Ex", PG, "shop", "orders")
    assert kwargs == {"via": "mcp", "named": catalog.named}


def test_the_mcp_tool_refuses_a_database_outside_the_grant(door, monkeypatch):
    with pytest.raises(tools.ToolError) as e:
        catalog.call("describe_table", {"connection": "srv-alpha", "database": "other",
                                        "table": "orders"})
    assert e.value.code == "no_access"
    assert door["calls"] == []


def test_the_mcp_tool_needs_a_table(door):
    with pytest.raises(tools.ToolError) as e:
        catalog.call("describe_table", {"connection": "srv-alpha", "database": "shop"})
    assert e.value.code == "bad_arguments"


def test_the_catalog_lists_the_tool_after_describe_database():
    names = [t["name"] for t in catalog.listing()]
    assert names.index("describe_table") == names.index("describe_database") + 1
    text = next(t for t in catalog.listing() if t["name"] == "describe_table")["description"]
    assert "numbers only" in text and "never returns a value" in text
    assert "`queryhub_describe_database`" in text
