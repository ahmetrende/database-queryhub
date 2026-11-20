"""What uses an object before a DROP or ALTER (ddl_dependents.py).

At submit, a DDL request that drops or alters an object reads the objects
that use it. The approval card and the web queue show them. The read never
fails a submit: any error gives None, which shows as "not checked".

Nothing here may reach a database. The connections are fakes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg
import pytest

from queryhub import (access, auto_approve, core_submit as cs, db,
                           ddl_dependents as dd, lifecycle, targets)
from queryhub.slack_app import notifications
from queryhub.web import mapping, routes_admin

USER = "U0EXAMPLE002"
DDL_EVERYWHERE = {"mode": "ddl", "allowed_databases": None, "source": "admin_or_bypass"}
LEDGER = targets.TargetServer(
    id=7, alias="example-pg", host="db.example.internal", port=5432,
    default_database="ledger", username="reader", enabled=True, notes=None,
    engine="postgres", engine_config={})


# --- targets_of --------------------------------------------------------------

def test_drop_table_names_the_table():
    assert dd.targets_of("DROP TABLE public.orders", "postgres") == [
        {"name": "public.orders", "kind": "table", "cascade": False}]


def test_drop_view_if_exists_cascade():
    got = dd.targets_of("DROP VIEW IF EXISTS public.v_orders CASCADE", "postgres")
    assert got == [{"name": "public.v_orders", "kind": "view", "cascade": True}]


def test_alter_table_drop_column_names_the_table():
    got = dd.targets_of("ALTER TABLE public.orders DROP COLUMN note", "postgres")
    assert got == [{"name": "public.orders", "kind": "table", "cascade": False}]


def test_drop_function_with_arguments_names_the_function():
    got = dd.targets_of("DROP FUNCTION f(int)", "postgres")
    assert [(o["name"], o["kind"]) for o in got] == [("f", "function")]


def test_drop_materialized_view():
    got = dd.targets_of("DROP MATERIALIZED VIEW app.mv_totals", "postgres")
    assert [(o["name"], o["kind"]) for o in got] == [("app.mv_totals", "materialized view")]


def test_a_select_names_nothing():
    assert dd.targets_of("SELECT * FROM public.orders", "postgres") == []


def test_create_names_nothing():
    assert dd.targets_of("CREATE INDEX idx_a ON public.orders (id)", "postgres") == []


def test_tsql_drop_table():
    got = dd.targets_of("DROP TABLE dbo.x", "mssql")
    assert got == [{"name": "dbo.x", "kind": "table", "cascade": False}]


def test_the_regex_fallback_reads_a_statement_sqlglot_cannot():
    got = dd._regex_targets("-- note\nDROP TABLE IF EXISTS a.b, c CASCADE", "postgres")
    assert got == [{"name": "a.b", "kind": "table", "cascade": True},
                   {"name": "c", "kind": "table", "cascade": True}]


def test_at_most_ten_objects():
    sql = "; ".join(f"DROP TABLE public.t{i}" for i in range(15))
    assert len(dd.targets_of(sql, "postgres")) == dd.MAX_OBJECTS


# --- read --------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, views, fks):
        self.views, self.fks, self.sql = views, fks, []
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = sql

    def fetchone(self):
        if "to_regclass" in self._last:
            return (101, "public", "orders")
        return None

    def fetchall(self):
        if "pg_rewrite" in self._last:
            return self.views
        if "pg_constraint" in self._last:
            return self.fks
        return []


class _FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur

    def rollback(self):
        pass


@pytest.fixture
def pg(monkeypatch):
    st = {"views": [], "fks": [], "kwargs": None}

    def connect(**kwargs):
        st["kwargs"] = kwargs
        st["cur"] = _FakeCursor(st["views"], st["fks"])
        return _FakeConn(st["cur"])

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(dd.targets, "get_credentials", lambda tid, mode: ("reader", "pw"))
    monkeypatch.setattr(dd.cfg, "target_ssl_kwargs", lambda host: {})
    return st


def test_read_returns_the_shape(pg):
    pg["views"].append(("v", "public", "v_orders"))
    pg["fks"].append(("fk_invoice_order", "billing", "invoices"))
    out = dd.read(LEDGER, "postgres",
                  [{"name": "public.orders", "kind": "table", "cascade": False}],
                  database="ledger")
    assert out == {"engine": "postgres", "bodiesChecked": False, "objects": [{
        "name": "public.orders", "kind": "table", "found": True, "cascade": False,
        "dependents": [
            {"kind": "view", "name": "public.v_orders"},
            {"kind": "foreign key", "name": "billing.invoices",
             "detail": "fk_invoice_order"}],
        "more": 0}]}
    assert pg["kwargs"]["dbname"] == "ledger"
    assert pg["kwargs"]["connect_timeout"] == 2
    assert "statement_timeout=2000" in pg["kwargs"]["options"]
    sqls = [s for s, _ in pg["cur"].sql]
    assert sqls[0] == "SET TRANSACTION READ ONLY"
    assert sqls[1].startswith("SET LOCAL search_path = ")
    # The name is a parameter, never part of the SQL text.
    assert all("public.orders" not in s for s in sqls)


def test_read_caps_dependents_at_25(pg):
    pg["views"].extend(("v", "public", f"v_{i:02d}") for i in range(30))
    pg["fks"].append(("fk_a", "billing", "invoices"))
    out = dd.read(LEDGER, "postgres",
                  [{"name": "public.orders", "kind": "table", "cascade": True}])
    obj = out["objects"][0]
    assert len(obj["dependents"]) == 25
    assert obj["more"] == 6
    assert obj["cascade"] is True


def test_read_returns_none_on_an_error(monkeypatch):
    def boom(**kwargs):
        raise psycopg.OperationalError("timeout expired")
    monkeypatch.setattr(psycopg, "connect", boom)
    monkeypatch.setattr(dd.targets, "get_credentials", lambda tid, mode: ("reader", "pw"))
    monkeypatch.setattr(dd.cfg, "target_ssl_kwargs", lambda host: {})
    assert dd.read(LEDGER, "postgres", [{"name": "t", "kind": "table"}]) is None


def test_read_returns_none_without_a_login(monkeypatch):
    def no_login(tid, mode):
        raise LookupError("no RO login")
    monkeypatch.setattr(dd.targets, "get_credentials", no_login)
    assert dd.read(LEDGER, "postgres", [{"name": "t", "kind": "table"}]) is None


def test_read_skips_another_engine():
    assert dd.read(LEDGER, "clickhouse", [{"name": "t", "kind": "table"}]) is None


class _FakeMssqlCursor:
    """Answers the SQL Server reads in the order ddl_dependents sends them."""

    def __init__(self, can_see: int):
        self.can_see = can_see
        self.sql: list[str] = []
        self._rows: list = []

    def execute(self, sql, params=()):
        self.sql.append(sql)
        if "HAS_PERMS_BY_NAME" in sql:
            self._rows = [(self.can_see,)]
        elif "OBJECT_ID(?)" in sql:
            self._rows = [(101,)]
        elif "sql_expression_dependencies" in sql:
            self._rows = [("V ", "dbo", "v_orders"), ("P ", "dbo", "p_close")]
        elif "foreign_keys" in sql:
            self._rows = [("fk_line_order", "dbo", "order_lines")]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, n):
        return self._rows[:n]


class _FakeMssqlConn:
    def __init__(self, cur):
        self.cur = cur
        self.closed = False

    def cursor(self):
        return self.cur

    def rollback(self):
        pass

    def close(self):
        self.closed = True


MSSQL = targets.TargetServer(
    id=8, alias="example-ms", host="ms.example.internal", port=1433,
    default_database="sales", username="reader", enabled=True, notes=None,
    engine="mssql", engine_config={})


@pytest.fixture
def ms(monkeypatch):
    from queryhub import mssql_exec
    state: dict = {}

    def fake_connect(host, port, database, user, password, **kw):
        state["kw"] = {"database": database, **kw}
        state["conn"] = _FakeMssqlConn(state["cur"])
        return state["conn"]
    monkeypatch.setattr(mssql_exec, "connect", fake_connect)
    monkeypatch.setattr(dd.targets, "get_credentials", lambda tid, mode: ("reader", "pw"))
    return state


def test_mssql_read_returns_the_shape(ms):
    ms["cur"] = _FakeMssqlCursor(can_see=1)
    out = dd.read(MSSQL, "mssql", [{"name": "dbo.orders", "kind": "table", "cascade": False}])
    assert out == {"engine": "mssql", "bodiesChecked": True, "objects": [{
        "name": "dbo.orders", "kind": "table", "found": True, "cascade": False,
        "dependents": [
            {"kind": "view", "name": "dbo.v_orders"},
            {"kind": "procedure", "name": "dbo.p_close"},
            {"kind": "foreign key", "name": "dbo.order_lines", "detail": "fk_line_order"}],
        "more": 0}]}
    assert ms["kw"]["database"] == "sales" and ms["kw"]["read_only"] is True
    assert ms["kw"]["timeout_sec"] <= 3
    assert ms["conn"].closed


def test_mssql_without_view_definition_is_not_checked(ms):
    """Without VIEW DEFINITION the dependency rows are hidden. An empty list
    would read as "nothing uses it", so the answer is None (not checked)."""
    ms["cur"] = _FakeMssqlCursor(can_see=0)
    assert dd.read(MSSQL, "mssql", [{"name": "dbo.orders", "kind": "table"}]) is None
    assert not any("sql_expression_dependencies" in s for s in ms["cur"].sql)
    assert ms["conn"].closed


# --- summary_text ------------------------------------------------------------

def _obj(deps, **kw):
    base = {"name": "public.orders", "kind": "table", "found": True,
            "cascade": False, "dependents": deps, "more": 0}
    base.update(kw)
    return {"engine": "postgres", "objects": [base], "bodiesChecked": False}


def test_summary_names_the_dependents():
    text = dd.summary_text(_obj([
        {"kind": "view", "name": "public.v_a"}, {"kind": "view", "name": "public.v_b"},
        {"kind": "foreign key", "name": "billing.invoices", "detail": "fk_x"}]))
    assert text == ("public.orders: used by 2 views (public.v_a, public.v_b) "
                    "and 1 foreign key (billing.invoices)")


def test_summary_names_at_most_five():
    deps = [{"kind": "view", "name": f"public.v_{i}"} for i in range(7)]
    text = dd.summary_text(_obj(deps, more=3))
    assert text == ("public.orders: used by 7 views (public.v_0, public.v_1, public.v_2, "
                    "public.v_3, public.v_4 and 2 more) and 3 more")


def test_summary_empty_missing_and_cascade():
    assert dd.summary_text(_obj([])) == "public.orders: nothing found that uses it"
    assert dd.summary_text(_obj([], found=False)) == "public.orders: object not found"
    text = dd.summary_text(_obj([{"kind": "view", "name": "public.v_a"}], cascade=True))
    assert text.endswith(". CASCADE drops these too")
    assert dd.summary_text(None) is None


def test_the_card_line_escapes_and_says_bodies_are_not_checked():
    md = notifications._ddl_dependents_md(_obj([{"kind": "view", "name": "a<b"}]))
    assert md.startswith("*Used by* (read at submit):\n")
    assert "a&lt;b" in md and "a<b" not in md
    assert md.endswith("_Code inside function bodies is not checked._")
    assert notifications._ddl_dependents_md(None) is None


# --- core_submit -------------------------------------------------------------

@pytest.fixture
def submit(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)
    monkeypatch.setattr(access, "roles", lambda pid: [])
    monkeypatch.setattr(auto_approve, "active_grants", lambda pid, at=None: [])
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.requesters, "open_request_count", lambda uid: 0)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(cs.targets, "get", lambda tid: LEDGER)
    monkeypatch.setattr(cs.teams, "effective_grant_for_user", lambda uid, tid: DDL_EVERYWHERE)
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "ddl")
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: True)
    monkeypatch.setattr(cs.pre_flight, "explain", lambda *a, **k: (True, None, None))
    monkeypatch.setattr(cs.pre_flight, "validate_statements",
                        lambda *a, **k: (True, None, None))
    monkeypatch.setattr(cs.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(cs.targets, "configured_tiers",
                        lambda ids: {i: {"ro", "rw", "ddl"} for i in ids})
    st = {"calls": []}
    result = {"engine": "postgres", "objects": [], "bodiesChecked": False}

    def fake_read(target, engine, objects, default_schema=None, *, database=None):
        st["calls"].append((engine, objects, database))
        return result

    monkeypatch.setattr(cs.ddl_dependents, "read", fake_read)

    def go(sql):
        return cs.validate_submission(
            USER, "Ex", target_server_id=LEDGER.id, database_name="ledger",
            query=sql, justification="remove the old report view", confirmed=True)
    st["go"], st["result"] = go, result
    return st


def test_a_ddl_drop_carries_the_dependents(submit):
    out = submit["go"]("DROP VIEW public.v_orders")
    assert isinstance(out, cs.Prepared), out
    assert out.ddl_dependents is submit["result"]
    engine, objects, database = submit["calls"][0]
    assert engine == "postgres" and database == "ledger"
    assert objects == [{"name": "public.v_orders", "kind": "view", "cascade": False}]


def test_a_read_does_not_read_dependents(submit):
    out = submit["go"]("SELECT * FROM ledgers")
    assert isinstance(out, cs.Prepared) and out.required_mode == "ro"
    assert out.ddl_dependents is None
    assert submit["calls"] == []


def test_the_switch_off_skips_the_read(submit, monkeypatch):
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: False)
    out = submit["go"]("DROP VIEW public.v_orders")
    assert isinstance(out, cs.Prepared) and out.ddl_dependents is None
    assert submit["calls"] == []


def test_the_request_row_carries_the_column():
    assert "ddl_dependents" in [c.strip() for c in cs.REQUEST_RETURNING.split(",")]
    assert cs.Prepared.__dataclass_fields__["ddl_dependents"].default is None
    assert "ddl_dependents" in routes_admin._QUEUE_COLS


# --- the web queue -----------------------------------------------------------

def _queue_row(value):
    return {"id": 9, "requester_slack_id": "U0EXAMPLE003", "requester_name": "Example",
            "target_server_id": 3, "database_name": "app",
            "query": "DROP VIEW public.v_orders", "justification": "cleanup",
            "risk_summary": None, "ddl_dependents": value,
            "created_at": datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)}


def test_queue_item_emits_ddl_dependents(monkeypatch):
    from queryhub import pii
    monkeypatch.setattr(pii, "column_pii_map", lambda cols: {})
    value = _obj([{"kind": "view", "name": "public.v_a"}])
    assert mapping.queue_item(_queue_row(value), lambda t: "conn")["ddlDependents"] == value
    assert mapping.queue_item(_queue_row(json.dumps(value)),
                              lambda t: "conn")["ddlDependents"] == value
    item = mapping.queue_item(_queue_row(None), lambda t: "conn")
    assert item["ddlDependents"] is None
    assert item["defaultSchema"] is None and item["escalate"] is True
