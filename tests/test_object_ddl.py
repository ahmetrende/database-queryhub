"""The CREATE script of one object (Devart B5): the route and the readers of
SQL Server, ClickHouse and Athena, with fakes. PostgreSQL runs against a real
server in tests/test_integration_object_ddl.py.
"""
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import athena_exec, clickhouse_exec, mssql_exec, pg_ddl
from queryhub.web import routes_data as rd


def _target(engine, **kw):
    return SimpleNamespace(id=7, alias="srv-alpha", engine=engine, host="h", port=1, **kw)


@pytest.fixture
def api(monkeypatch):
    calls = {"audit": []}
    monkeypatch.setattr(rd.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rd, "_ro_login", lambda t: ("ro", "pw", None))
    from queryhub import audit
    monkeypatch.setattr(audit, "log", lambda *a, **k: calls["audit"].append(a))
    state = {"t": _target("mssql")}
    monkeypatch.setattr(rd, "_granted_db", lambda uid, conn, db: (state["t"], "ro") if conn == "srv-alpha" else (None, None))

    def ms(host, port, db, user, pw, schema, name):
        calls["ms"] = (db, schema, name)
        return ("view", "CREATE VIEW dbo.v AS SELECT 1", False) if name == "v" else None
    monkeypatch.setattr(mssql_exec, "object_ddl", ms)

    def ch(host, port, db, user, pw, name):
        calls["ch"] = (db, name)
        return ("table", "CREATE TABLE shop.t (a UInt8) ENGINE = Memory", False)
    monkeypatch.setattr(clickhouse_exec, "object_ddl", ch)
    monkeypatch.setattr(athena_exec, "config_of", lambda t: {"catalog": "AwsDataCatalog"})

    def at(cfg, db, name):
        calls["at"] = (db, name)
        return ("table", "CREATE EXTERNAL TABLE x", True)
    monkeypatch.setattr(athena_exec, "object_ddl", at)
    return calls, state


def _get(name, conn="srv-alpha", db="shop"):
    try:
        return rd.object_ddl(conn, db, name, claims={"sub": "U0DDLTEST1", "name": "me"})
    except HTTPException as e:
        return (e.status_code, e.detail["code"], e.detail["message"])


def test_the_script_comes_back_and_the_read_is_audited(api):
    calls, _ = api
    out = _get("dbo.v")
    assert out == {"name": "dbo.v", "kind": "view", "ddl": "CREATE VIEW dbo.v AS SELECT 1", "built": False}
    assert calls["ms"] == ("shop", "dbo", "v")
    assert calls["audit"] == [(None, "U0DDLTEST1", "me", "object_ddl_read",
                               {"target_id": 7, "database": "shop", "object": "dbo.v", "kind": "view"})]


def test_no_grant_no_object_and_a_bad_name(api):
    calls, _ = api
    assert _get("dbo.v", conn="other")[:2] == (404, "not_found")
    assert _get("dbo.nothing")[:2] == (404, "not_found")
    assert _get("   ")[:2] == (422, "invalid")
    assert _get("x" * 301)[:2] == (422, "invalid")
    assert calls["audit"] == [], "nothing read, nothing audited"


@pytest.mark.parametrize("engine, name", [
    ("mssql", "otherdb.dbo.v"), ("mssql", "a.b.c.d"),
    ("clickhouse", "otherdb.t"), ("clickhouse", "a.b.c"),
    ("athena", "otherdb.t"), ("athena", "othercat.shop.t"),
    ("postgres", "otherdb.public.t"),
])
def test_a_name_of_another_database_is_a_missing_object(api, engine, name):
    calls, state = api
    state["t"] = _target(engine)
    assert _get(name)[:2] == (404, "not_found")
    assert not ({"ms", "ch", "at"} & set(calls)), "no reader ran"


def test_each_engine_gets_its_reader(api):
    calls, state = api
    state["t"] = _target("clickhouse")
    assert _get("shop.t")["ddl"].startswith("CREATE TABLE shop.t")
    assert calls["ch"] == ("shop", "t")
    state["t"] = _target("athena")
    assert _get("AwsDataCatalog.Shop.Orders")["built"] is True
    assert calls["at"] == ("shop", "orders"), "Athena folds names to lower case"
    state["t"] = _target("mysql")
    assert _get("t")[:2] == (422, "unsupported")


def test_a_driver_error_gets_a_fixed_sentence(api, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("login failed for ro@secret-host")
    monkeypatch.setattr(mssql_exec, "object_ddl", boom)
    status, code, msg = _get("dbo.v")
    assert (status, code) == (503, "server_error")
    assert "secret-host" not in msg


def test_no_read_login(api, monkeypatch):
    monkeypatch.setattr(rd, "_ro_login", lambda t: (None, None, "missing"))
    assert _get("dbo.v")[:2] == (503, "server_error")


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

class _Cur:
    def __init__(self, answers):
        self.answers, self.q = answers, []

    def execute(self, sql, *params):
        self.q.append(sql)

    def _next(self):
        return self.answers.pop(0)

    def fetchone(self):
        rows = self._next()
        return rows[0] if rows else None

    def fetchall(self):
        return self._next()


def test_sql_server_builds_a_table_and_returns_a_view_text(monkeypatch):
    answers = [
        [("U ", None, "dbo", "Order Lines")],
        [("id", "int", 4, 10, 0, False, True, None),
         ("note", "nvarchar", 100, 0, 0, True, False, None),
         ("big", "varchar", -1, 0, 0, True, False, None),
         ("amount", "decimal", 9, 12, 2, False, False, "((0))"),
         ("at", "datetime2", 8, 27, 3, False, False, "(getdate())")],
        [("PK_lines", "id")],
    ]
    cur = _Cur(answers)
    monkeypatch.setattr(mssql_exec, "connect", lambda *a, **k: SimpleNamespace(cursor=lambda: cur, close=lambda: None))
    kind, script, built = mssql_exec.object_ddl("h", 1, "shop", "u", "p", "dbo", "Order Lines")
    assert (kind, built) == ("table", True)
    assert script == ("CREATE TABLE [dbo].[Order Lines] (\n"
                      "    [id] int IDENTITY NOT NULL,\n"
                      "    [note] nvarchar(50) NULL,\n"
                      "    [big] varchar(max) NULL,\n"
                      "    [amount] decimal(12, 2) NOT NULL DEFAULT ((0)),\n"
                      "    [at] datetime2(3) NOT NULL DEFAULT (getdate()),\n"
                      "    CONSTRAINT [PK_lines] PRIMARY KEY ([id])\n);")
    cur2 = _Cur([[("P ", "CREATE PROCEDURE dbo.p AS SELECT 1\n", "dbo", "p")]])
    monkeypatch.setattr(mssql_exec, "connect", lambda *a, **k: SimpleNamespace(cursor=lambda: cur2, close=lambda: None))
    assert mssql_exec.object_ddl("h", 1, "shop", "u", "p", None, "p") == ("procedure", "CREATE PROCEDURE dbo.p AS SELECT 1", False)
    cur3 = _Cur([[]])
    monkeypatch.setattr(mssql_exec, "connect", lambda *a, **k: SimpleNamespace(cursor=lambda: cur3, close=lambda: None))
    assert mssql_exec.object_ddl("h", 1, "shop", "u", "p", None, "nothing") is None
    cur4 = _Cur([[("C ", "([total]>=(0))", "dbo", "ck_total")]])
    monkeypatch.setattr(mssql_exec, "connect", lambda *a, **k: SimpleNamespace(cursor=lambda: cur4, close=lambda: None))
    assert mssql_exec.object_ddl("h", 1, "shop", "u", "p", None, "ck_total") is None, \
        "a check constraint has a text, but it is no object to script"


def test_clickhouse_returns_the_server_text(monkeypatch):
    seen = {}

    class C:
        def execute(self, sql, params):
            seen["p"] = params
            return [("MaterializedView", "CREATE MATERIALIZED VIEW shop.mv AS SELECT 1\n")]

        def disconnect(self):
            seen["closed"] = True
    monkeypatch.setattr(clickhouse_exec, "client", lambda *a, **k: C())
    assert clickhouse_exec.object_ddl("h", 1, "shop", "u", "p", "mv") == (
        "materialized view", "CREATE MATERIALIZED VIEW shop.mv AS SELECT 1", False)
    assert seen == {"p": {"db": "shop", "t": "mv"}, "closed": True}


def test_athena_builds_a_table_and_decodes_a_view(monkeypatch):
    table = {"Name": "orders", "TableType": "EXTERNAL_TABLE", "StorageDescriptor": {
        "Columns": [{"Name": "id", "Type": "bigint"}, {"Name": "total", "Type": "double"}],
        "SerdeInfo": {"SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"},
        "InputFormat": "in.Format", "OutputFormat": "out.Format", "Location": "s3://bucket-x/orders/"},
        "PartitionKeys": [{"Name": "month", "Type": "string"}]}
    blob = base64.b64encode(json.dumps({"originalSql": "SELECT id FROM orders"}).encode()).decode()
    view = {"Name": "v", "TableType": "VIRTUAL_VIEW", "ViewOriginalText": f"/* Presto View: {blob} */"}
    tables = {"orders": table, "v": view}

    class G:
        def get_table(self, DatabaseName, Name):
            if Name not in tables:
                raise type("E", (Exception,), {"response": {"Error": {"Code": "EntityNotFoundException"}}})()
            return {"Table": tables[Name]}
    monkeypatch.setattr(athena_exec, "_glue", lambda cfg: G())
    kind, script, built = athena_exec.object_ddl({}, "shop", "orders")
    assert (kind, built) == ("table", True)
    assert script == ("CREATE EXTERNAL TABLE `shop`.`orders` (\n  `id` bigint,\n  `total` double\n)\n"
                      "PARTITIONED BY (\n  `month` string\n)\n"
                      "ROW FORMAT SERDE 'org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe'\n"
                      "STORED AS INPUTFORMAT 'in.Format'\n  OUTPUTFORMAT 'out.Format'\n"
                      "LOCATION 's3://bucket-x/orders/';")
    assert athena_exec.object_ddl({}, "shop", "v") == ("view", "CREATE OR REPLACE VIEW `shop`.`v` AS\nSELECT id FROM orders", True)
    assert athena_exec.object_ddl({}, "shop", "nothing") is None
    tables["bad"] = {"Name": "bad", "TableType": "VIRTUAL_VIEW", "ViewOriginalText": "/* Presto View: !!! */"}
    assert athena_exec.object_ddl({}, "shop", "bad") is None


def test_postgres_names_fold_like_the_server():
    assert pg_ddl.name_parts("Film") == ["film"]
    assert pg_ddl.name_parts('public."My T"') == ["public", "My T"]
    assert pg_ddl.name_parts(' s . t ') == ["s", "t"]
    assert pg_ddl.name_parts('"x""y"') == ['x"y']
    for bad in ("a..b", "a.", '"open', "a b", ""):
        assert pg_ddl.name_parts(bad) is None, bad
    assert [pg_ddl.quote_ident(n) for n in ("film", "My T", "t1", "_x", "1a")] == ["film", '"My T"', "t1", '"_x"', '"1a"']
