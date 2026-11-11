"""GET .../columns on SQL Server, ClickHouse and Athena targets.

The editor's `SELECT *` expansion asks here when the schema catalog does not
list a relation yet. Each engine has a reader of its own, and the route splits
the name before it reaches one. What matters:

* the read always names the granted database, never one from the name;
* a name qualified with another database is a 404, and nothing connects;
* the name reaches the server as a parameter (or an API argument), never SQL;
* a driver error is a 503 with a fixed sentence, not the driver's text.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import athena_exec, clickhouse_exec, mssql_exec
from queryhub.web import routes_data as rd

CLAIMS = {"sub": "U0AB12CD34"}


def _target(engine, **extra):
    return SimpleNamespace(id=9, alias="srv-alpha", host="db.example.internal",
                           port=1433, engine=engine, enabled=True,
                           engine_config=extra.get("engine_config"))


@pytest.fixture
def env(monkeypatch):
    state = {"target": _target("mssql"), "login_asked": 0}
    monkeypatch.setattr(rd.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rd, "_granted_db",
                        lambda uid, conn, dbname: (state["target"], {"mode": "ro"}))

    def _login(t):
        state["login_asked"] += 1
        return ("queryhub_ro", "pw", None)
    monkeypatch.setattr(rd, "_ro_login", _login)
    return state


def _call(table, dbname="shop"):
    return rd.table_columns("srv-alpha", dbname, table, CLAIMS)


def _status(exc_info):
    return exc_info.value.status_code, exc_info.value.detail["code"]


# ---- the name parser ---------------------------------------------------------

@pytest.mark.parametrize("name, parts", [
    ("orders", ["orders"]),
    ("dbo.orders", ["dbo", "orders"]),
    ("[dbo].[Order Lines]", ["dbo", "Order Lines"]),
    ('"Odd""Name".x', ['Odd"Name', "x"]),
    ("`ev``ents`", ["ev`ents"]),
    ("[a]]b]", ["a]b"]),
    ("shop . dbo . t", ["shop", "dbo", "t"]),
])
def test_names_split_into_unquoted_parts(name, parts):
    assert rd._name_parts(name) == parts


@pytest.mark.parametrize("name", ["db..t", "a.", ".a", '""', "[]", "a b", "[open"])
def test_a_name_that_does_not_parse_is_none(name):
    assert rd._name_parts(name) is None


# ---- SQL Server --------------------------------------------------------------

class _MssqlConn:
    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.executed, self.closed = rows, fail, [], False

    def cursor(self):
        return self

    def execute(self, sql, *params):
        self.executed.append((sql, params))
        if self.fail:
            raise RuntimeError("[Microsoft][ODBC Driver 18] login failed for host db.example.internal")

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


@pytest.fixture
def mssql(env, monkeypatch):
    env["target"] = _target("mssql")
    st = {"conn": _MssqlConn([("id", "int"), ("total", "decimal")]), "connect": None}

    def _connect(host, port, database, user, password, **kw):
        st["connect"] = {"database": database, "user": user, **kw}
        return st["conn"]
    monkeypatch.setattr(mssql_exec, "connect", _connect)
    return st


def test_mssql_reads_sys_columns_in_the_granted_database(mssql):
    out = _call("dbo.orders")
    assert out == {"table": "dbo.orders", "columns": [
        {"name": "id", "type": "int"}, {"name": "total", "type": "decimal"}]}
    assert mssql["connect"]["database"] == "shop"
    assert mssql["connect"]["user"] == "queryhub_ro"
    assert mssql["connect"]["timeout_sec"] <= 5
    sql, params = mssql["conn"].executed[0]
    assert sql == mssql_exec._LIVE_COLUMNS_SQL
    assert "OBJECT_ID(?)" in sql
    assert params == ("[dbo].[orders]",)
    assert mssql["conn"].closed


def test_mssql_brackets_every_part_so_the_name_never_becomes_sql(mssql):
    _call("[dbo].[x]]; DROP TABLE y]")
    assert mssql["conn"].executed[0][1] == ("[dbo].[x]]; DROP TABLE y]",)


def test_mssql_one_part_name_has_no_schema(mssql):
    _call("orders")
    assert mssql["conn"].executed[0][1] == ("[orders]",)


def test_mssql_three_part_name_in_the_same_database_drops_the_database(mssql):
    _call("SHOP.dbo.orders")
    assert mssql["conn"].executed[0][1] == ("[dbo].[orders]",)


@pytest.mark.parametrize("name", ["otherdb.dbo.orders", "[otherdb].[dbo].[orders]",
                                  "srv.shop.dbo.orders", "otherdb..orders"])
def test_mssql_another_database_is_404_and_never_connects(mssql, env, name):
    with pytest.raises(HTTPException) as e:
        _call(name)
    assert _status(e) == (404, "not_found")
    assert mssql["connect"] is None
    assert env["login_asked"] == 0


def test_mssql_no_such_table_is_404(mssql):
    mssql["conn"] = _MssqlConn([])
    with pytest.raises(HTTPException) as e:
        _call("dbo.nope")
    assert _status(e) == (404, "not_found")


def test_mssql_driver_error_is_503_without_the_driver_text(mssql):
    mssql["conn"] = _MssqlConn([], fail=True)
    with pytest.raises(HTTPException) as e:
        _call("dbo.orders")
    assert _status(e) == (503, "server_error")
    assert "example" not in e.value.detail["message"]
    assert "ODBC" not in e.value.detail["message"]
    assert mssql["conn"].closed


def test_mssql_without_a_read_login_is_503(mssql, monkeypatch):
    monkeypatch.setattr(rd, "_ro_login", lambda t: ("", "", "not configured"))
    with pytest.raises(HTTPException) as e:
        _call("dbo.orders")
    assert _status(e) == (503, "server_error")
    assert "read credential" in e.value.detail["message"]
    assert mssql["connect"] is None


# ---- ClickHouse --------------------------------------------------------------

class _ChClient:
    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.executed, self.closed = rows, fail, [], False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.fail:
            raise RuntimeError("Code: 516. DB::Exception: queryhub_ro: Authentication failed")
        return self.rows

    def disconnect(self):
        self.closed = True


@pytest.fixture
def ch(env, monkeypatch):
    env["target"] = _target("clickhouse")
    st = {"client": _ChClient([("ts", "DateTime"), ("kind", "LowCardinality(String)")]),
          "made": None}

    def _client(host, port, database, user, password, **kw):
        st["made"] = {"database": database, "user": user, **kw}
        return st["client"]
    monkeypatch.setattr(clickhouse_exec, "client", _client)
    return st


def test_clickhouse_reads_system_columns_for_the_granted_database(ch):
    out = _call("events")
    assert [c["name"] for c in out["columns"]] == ["ts", "kind"]
    sql, params = ch["client"].executed[0]
    assert "system.columns" in sql and "%(db)s" in sql and "%(t)s" in sql
    assert params == {"db": "shop", "t": "events"}
    assert ch["made"]["database"] == "shop"
    assert ch["client"].closed


def test_clickhouse_same_database_prefix_is_read(ch):
    _call("shop.events")
    assert ch["client"].executed[0][1] == {"db": "shop", "t": "events"}


@pytest.mark.parametrize("name", ["system.users", "otherdb.events", "Shop.events",
                                  "a.b.c"])
def test_clickhouse_another_database_is_404_and_never_connects(ch, env, name):
    # ClickHouse database names keep their case: `Shop` is not `shop`.
    with pytest.raises(HTTPException) as e:
        _call(name)
    assert _status(e) == (404, "not_found")
    assert ch["made"] is None
    assert env["login_asked"] == 0


def test_clickhouse_driver_error_is_503_without_the_driver_text(ch):
    ch["client"] = _ChClient([], fail=True)
    with pytest.raises(HTTPException) as e:
        _call("events")
    assert _status(e) == (503, "server_error")
    assert "queryhub_ro" not in e.value.detail["message"]
    assert ch["client"].closed


def test_clickhouse_no_such_table_is_404(ch):
    ch["client"] = _ChClient([])
    with pytest.raises(HTTPException) as e:
        _call("nope")
    assert _status(e) == (404, "not_found")


# ---- Athena ------------------------------------------------------------------

class _NotFound(Exception):
    response = {"Error": {"Code": "EntityNotFoundException"}}


class _Glue:
    def __init__(self, table=None, fail=None):
        self.table, self.fail, self.asked = table, fail, []

    def get_table(self, DatabaseName, Name):             # noqa: N803
        self.asked.append((DatabaseName, Name))
        if self.fail:
            raise self.fail
        if self.table is None:
            raise _NotFound()
        return {"Table": self.table}


_GLUE_TABLE = {
    "Name": "trades",
    "StorageDescriptor": {"Columns": [{"Name": "id", "Type": "bigint"},
                                      {"Name": "price", "Type": "decimal(38,18)"}]},
    "PartitionKeys": [{"Name": "month", "Type": "string"}],
}
_CFG = {"region": "eu-west-1", "workgroup": "wg", "database": "shop",
        "role_arn": "arn:aws:iam::000000000000:role/example"}


@pytest.fixture
def athena(env, monkeypatch):
    env["target"] = _target("athena", engine_config=dict(_CFG))
    st = {"glue": _Glue(_GLUE_TABLE)}
    monkeypatch.setattr(athena_exec, "_glue", lambda cfg: st["glue"])
    return st


def test_athena_reads_glue_with_partition_keys_last(athena, env):
    out = _call("trades")
    assert out["columns"] == [{"name": "id", "type": "bigint"},
                              {"name": "price", "type": "decimal(38,18)"},
                              {"name": "month", "type": "string"}]
    assert athena["glue"].asked == [("shop", "trades")]
    assert env["login_asked"] == 0, "Athena has no password; the role is in engine_config"


@pytest.mark.parametrize("name", ['"Shop"."TRADES"', "AwsDataCatalog.shop.trades",
                                  "awsdatacatalog.SHOP.Trades"])
def test_athena_folds_names_to_lower_case(athena, name):
    _call(name)
    assert athena["glue"].asked == [("shop", "trades")]


@pytest.mark.parametrize("name", ["otherdb.trades", "othercatalog.shop.trades",
                                  "a.b.c.d"])
def test_athena_another_database_or_catalog_is_404_and_never_asks_glue(athena, name):
    with pytest.raises(HTTPException) as e:
        _call(name)
    assert _status(e) == (404, "not_found")
    assert athena["glue"].asked == []


def test_athena_no_such_table_is_404(athena):
    athena["glue"] = _Glue(None)
    with pytest.raises(HTTPException) as e:
        _call("nope")
    assert _status(e) == (404, "not_found")


def test_athena_other_glue_error_is_503_without_the_arn(athena):
    athena["glue"] = _Glue(_GLUE_TABLE, fail=RuntimeError(
        "AccessDenied for arn:aws:iam::000000000000:role/example"))
    with pytest.raises(HTTPException) as e:
        _call("trades")
    assert _status(e) == (503, "server_error")
    assert "arn:" not in e.value.detail["message"]


def test_athena_missing_config_is_503(athena, env):
    env["target"] = _target("athena", engine_config={"region": "eu-west-1"})
    with pytest.raises(HTTPException) as e:
        _call("trades")
    assert _status(e) == (503, "server_error")
    assert athena["glue"].asked == []
