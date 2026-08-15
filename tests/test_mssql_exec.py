"""MSSQL ODBC connection-string builder — pure (no pyodbc / driver / DB)."""
import pytest

from dba_slack_bot import config as cfg
from dba_slack_bot import mssql_exec as mx


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    # Keep the builder tests off the metadata DB (get_setting/get_bool).
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: d)
    monkeypatch.setattr(cfg, "get_bool", lambda k, d=False: d)


def test_connstring_basics():
    cs = mx.odbc_connection_string("h.example.com", 1433, "SalesDB", "svc", "pw")
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in cs
    assert "SERVER=h.example.com,1433" in cs
    assert "DATABASE={SalesDB}" in cs
    assert "UID={svc}" in cs
    assert "PWD={pw}" in cs
    assert "Encrypt=yes" in cs                  # TLS on by default
    assert "TrustServerCertificate=no" in cs    # cert verified by default
    assert "Connection Timeout=15" in cs


def test_connstring_braces_special_password():
    # A password containing ; and } must not break out of the PWD field —
    # `}` is escaped by doubling, the whole value is brace-wrapped.
    cs = mx.odbc_connection_string("h", 1433, "d", "u", "p;a}b")
    assert "PWD={p;a}}b}" in cs


def test_connstring_trust_cert_opt_in():
    cs = mx.odbc_connection_string("h", 1433, "d", "u", "p", trust_server_cert=True)
    assert "TrustServerCertificate=yes" in cs


def test_connstring_driver_override():
    cs = mx.odbc_connection_string("h", 1433, "d", "u", "p",
                                   driver="ODBC Driver 17 for SQL Server")
    assert "DRIVER={ODBC Driver 17 for SQL Server}" in cs


# --- Availability Group listener routing ------------------------------------

def test_read_only_intent_routes_to_secondary():
    # RO tier → ApplicationIntent=ReadOnly so the AG listener routes reads
    # to a readable secondary.
    cs = mx.odbc_connection_string("listener", 1433, "d", "u", "p",
                                   application_intent="ReadOnly")
    assert "ApplicationIntent=ReadOnly" in cs


def test_no_intent_by_default_hits_primary():
    # RW/DDL (no intent) → primary. Never emit ApplicationIntent unasked.
    cs = mx.odbc_connection_string("listener", 1433, "d", "u", "p")
    assert "ApplicationIntent" not in cs


def test_multi_subnet_failover_default_on():
    cs = mx.odbc_connection_string("listener", 1433, "d", "u", "p")
    assert "MultiSubnetFailover=yes" in cs
    off = mx.odbc_connection_string("listener", 1433, "d", "u", "p",
                                    multi_subnet_failover=False)
    assert "MultiSubnetFailover" not in off


# --- bot-DB host map (read-only routing without /etc/hosts) ------------------

def test_load_host_map_keyed_lowercase(monkeypatch):
    from dba_slack_bot import db as _db
    monkeypatch.setattr(_db, "fetch_all", lambda *a, **k: [
        {"server_name": "TS-SQL0", "ip": "203.0.113.10"},
        {"server_name": "TS-SQL1", "ip": "203.0.113.11"}])
    assert mx.load_host_map() == {"ts-sql0": "203.0.113.10", "ts-sql1": "203.0.113.11"}


def test_resolve_ro_endpoint_none_without_map(monkeypatch):
    # Empty map → None (fall back to primary), before any connection attempt.
    monkeypatch.setattr(mx, "load_host_map", lambda: {})
    assert mx.resolve_ro_endpoint("listener", 1433, "d", "u", "p") is None


# --- schema catalog reader (normalization; no pyodbc / driver / DB) ----------

class _FakeCursor:
    """Returns a scripted (description, rows) for whichever SQL marker matches
    first — so one fake serves the two-query catalog_snapshot."""
    def __init__(self, script):  # script: list of (marker, description, rows)
        self._script = script
        self._desc, self._rows = None, []

    def execute(self, sql):
        for marker, desc, rows in self._script:
            if marker in sql:
                self._desc, self._rows = desc, rows
                return
        raise AssertionError(f"unexpected SQL: {sql[:60]!r}")

    @property
    def description(self):
        return self._desc

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cursor):
        self._cur, self.closed = cursor, False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


def test_catalog_databases_lists_names(monkeypatch):
    desc = [("name",)]
    rows = [("DBA",), ("gtpbrdb",), ("XHCP",)]
    conn = _FakeConn(_FakeCursor([("sys.databases", desc, rows)]))
    monkeypatch.setattr(mx, "connect", lambda *a, **k: conn)
    assert mx.catalog_databases("h", 1433, "DBA", "u", "p") == ["DBA", "gtpbrdb", "XHCP"]
    assert conn.closed  # connection always closed


def test_catalog_snapshot_normalizes_shape(monkeypatch):
    tdesc = [("schema_name",), ("table_name",), ("relkind",),
             ("row_estimate",), ("total_bytes",)]
    trows = [("dbo", "Orders", "table", 10, 16384),
             ("dbo", "VOrders", "view", None, None)]
    cdesc = [("schema_name",), ("table_name",), ("ordinal",), ("column_name",),
             ("data_type",), ("not_null",), ("default_expr",), ("is_pk",),
             ("in_index",)]
    crows = [("dbo", "Orders", 1, "Id", "int", 1, None, 1, 1),
             ("dbo", "Orders", 2, "Note", "nvarchar(50)", 0, None, 0, 0)]
    idesc = fdesc = [("schema_name",), ("table_name",), ("name",), ("def",)]
    irows = [("dbo", "Orders", "PK_Orders", "(Id) UNIQUE PRIMARY KEY")]
    frows = [("dbo", "Orders", "FK_Orders_User", "(UserId) -> dbo.Users")]
    # Each of the four catalog queries is matched by a marker unique to its SQL.
    conn = _FakeConn(_FakeCursor([
        ("sys.allocation_units", tdesc, trows),       # tables
        ("sys.types", cdesc, crows),                  # columns
        ("is_included_column", idesc, irows),         # indexes
        ("sys.foreign_key_columns", fdesc, frows),    # foreign keys
    ]))
    monkeypatch.setattr(mx, "connect", lambda *a, **k: conn)

    tables, columns = mx.catalog_snapshot("h", 1433, "gtpbrdb", "u", "p")
    assert conn.closed
    # tables: relkind passthrough + partition fields None
    assert [t["relkind"] for t in tables] == ["table", "view"]
    assert tables[0]["row_estimate"] == 10 and tables[0]["total_bytes"] == 16384
    assert tables[0]["partition_count"] is None and tables[0]["partition_key"] is None
    # indexes / foreign_keys attach to their table (jsonb-able); None elsewhere
    assert tables[0]["indexes"] == [{"name": "PK_Orders", "def": "(Id) UNIQUE PRIMARY KEY"}]
    assert tables[0]["foreign_keys"] == [{"name": "FK_Orders_User", "def": "(UserId) -> dbo.Users"}]
    assert tables[1]["indexes"] is None and tables[1]["foreign_keys"] is None
    # columns: 1/0 coerced to real bools; data_type carries the modifier
    assert columns[0]["not_null"] is True and columns[0]["is_pk"] is True
    assert columns[0]["in_index"] is True
    assert columns[1]["is_pk"] is False and columns[1]["in_index"] is False
    assert columns[1]["data_type"] == "nvarchar(50)"
