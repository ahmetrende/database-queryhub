"""GET /connections/{conn}/databases/{db}/columns — one relation's columns, live.

The editor's `SELECT *` expansion asks here when the schema catalog does not
list the relation in FROM (a partition, a table newer than the snapshot). What
matters is the shape of the read: the caller's grant decides, the query is
fixed and parameterised (the name never becomes SQL), the connection is READ
ONLY with a statement timeout, and only PostgreSQL is asked.
"""
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import HTTPException

from queryhub.web import routes_data as rd

CLAIMS = {"sub": "U0AB12CD34"}
TARGET = SimpleNamespace(id=7, host="db.example.internal", port=5432,
                         engine="postgres", enabled=True)


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if self.conn.raise_on_query:
            raise psycopg.OperationalError("boom")

    def fetchall(self):
        return self.conn.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows, raise_on_query=False):
        self.rows, self.raise_on_query, self.executed = rows, raise_on_query, []

    def cursor(self):
        return _Cursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def live(monkeypatch):
    state = {"conn": _Conn([("tx_id", "bigint"), ("user_id", "bigint")]),
             "connect_kwargs": None, "granted": TARGET}
    monkeypatch.setattr(rd.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rd, "_granted_db",
                        lambda uid, conn, dbname: (state["granted"], {"mode": "ro"})
                        if state["granted"] else (None, None))
    monkeypatch.setattr(rd, "_ro_login", lambda t: ("queryhub_ro", "pw", None))
    monkeypatch.setattr(rd.cfg, "target_ssl_kwargs", lambda host: {})

    def _connect(**kw):
        state["connect_kwargs"] = kw
        return state["conn"]
    monkeypatch.setattr(rd.psycopg, "connect", _connect)
    return state


def _call(table="ledgers_v2_p55", conn="prod-main", dbname="app"):
    return rd.table_columns(conn, dbname, table, CLAIMS)


def _status(exc_info):
    return exc_info.value.status_code, exc_info.value.detail["code"]


def test_columns_come_back_in_table_order(live):
    out = _call()
    assert out == {"table": "ledgers_v2_p55",
                   "columns": [{"name": "tx_id", "type": "bigint"},
                               {"name": "user_id", "type": "bigint"}]}


def test_the_read_is_read_only_fixed_and_parameterised(live):
    _call(table="public.\"Odd\"; DROP TABLE x")
    executed = live["conn"].executed
    assert executed[0] == ("SET TRANSACTION READ ONLY", None)
    sql, params = executed[1]
    assert sql == rd._LIVE_COLUMNS_SQL
    assert "to_regclass(%s)" in sql
    assert params == ("public.\"Odd\"; DROP TABLE x",), "the name is a parameter, never SQL"
    kw = live["connect_kwargs"]
    assert kw["dbname"] == "app"
    assert kw["user"] == "queryhub_ro"
    assert "statement_timeout" in kw["options"]
    assert kw["connect_timeout"] <= 5


def test_no_such_relation_is_404(live):
    live["conn"] = _Conn([])
    with pytest.raises(HTTPException) as e:
        _call(table="no_such_table")
    assert _status(e) == (404, "not_found")


def test_a_database_outside_the_grant_is_404_and_never_connects(live):
    live["granted"] = None
    with pytest.raises(HTTPException) as e:
        _call()
    assert _status(e) == (404, "not_found")
    assert live["connect_kwargs"] is None


def test_only_postgres_is_asked(live):
    live["granted"] = SimpleNamespace(**{**TARGET.__dict__, "engine": "mssql"})
    with pytest.raises(HTTPException) as e:
        _call()
    assert _status(e) == (422, "unsupported")
    assert live["connect_kwargs"] is None


@pytest.mark.parametrize("name", ["", "   ", "x" * 301])
def test_a_missing_or_absurd_name_is_refused(live, name):
    with pytest.raises(HTTPException) as e:
        _call(table=name)
    assert _status(e) == (422, "invalid")
    assert live["connect_kwargs"] is None


def test_no_read_login_is_503(live, monkeypatch):
    monkeypatch.setattr(rd, "_ro_login", lambda t: ("", "", "not configured"))
    with pytest.raises(HTTPException) as e:
        _call()
    assert _status(e) == (503, "server_error")
    assert live["connect_kwargs"] is None


def test_a_failing_target_is_503(live):
    live["conn"] = _Conn([], raise_on_query=True)
    with pytest.raises(HTTPException) as e:
        _call()
    assert _status(e) == (503, "server_error")


def test_the_route_is_registered_under_api():
    paths = {getattr(r, "path", "") for r in rd.router.routes}
    assert "/api/connections/{conn}/databases/{dbname}/columns" in paths
