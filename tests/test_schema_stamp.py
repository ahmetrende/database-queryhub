"""The catalog stamp: {snapshotAt, sig} of one database's catalog.

The browser asks GET .../schema/stamp every five minutes and reloads /schema
only when `sig` changed. What matters:

* one aggregate query on the bot DB, never the target;
* the signature tells a content change, the hourly snapshot time does not;
* /schema carries the same stamp, read BEFORE the tables, so a snapshot that
  lands between the two reads makes the browser reload at its next check;
* the stamp route needs the same grant as /schema.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import schema_catalog
from queryhub.web import routes_data as rd

CLAIMS = {"sub": "U0AB12CD34"}
TARGET = SimpleNamespace(id=7, alias="srv-alpha", host="db.example.internal",
                         port=5432, engine="postgres", enabled=True)
AT = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)


# ---- catalog_stamp -------------------------------------------------------------

def test_the_stamp_is_one_aggregate_query_on_the_bot_db(monkeypatch):
    seen = []
    monkeypatch.setattr(schema_catalog.db, "fetch_one",
                        lambda sql, params=None: seen.append((sql, params)) or
                        {"ts": AT, "n": 40760, "h": -1169593284503936901834})
    out = schema_catalog.catalog_stamp(7, "shop")
    assert out == {"snapshotAt": AT, "sig": "40760:-1169593284503936901834"}
    sql, params = seen[0]
    assert params == (7, "shop")
    assert "hashtextextended" in sql
    assert "LEFT JOIN schema_columns" in sql, "a table with no columns still counts"
    for part in ("st.schema_name", "st.table_name", "st.relkind", "c.column_name", "c.data_type"):
        assert part in sql, part + " must change the signature"
    assert "snapshot_at" not in sql.split("hashtextextended")[1], \
        "the hourly snapshot time must not change the signature"


@pytest.mark.parametrize("row", [None, {"ts": None, "n": 0, "h": 0}])
def test_an_empty_catalog_has_a_fixed_stamp(monkeypatch, row):
    monkeypatch.setattr(schema_catalog.db, "fetch_one", lambda sql, params=None: row)
    assert schema_catalog.catalog_stamp(7, "shop") == {"snapshotAt": None, "sig": "0"}


# ---- the routes ----------------------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    state = {"granted": TARGET, "order": []}
    monkeypatch.setattr(rd.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rd, "_granted_db",
                        lambda uid, conn, dbname: (state["granted"], {"mode": "ro"})
                        if state["granted"] else (None, None))

    def _stamp(tid, dbname):
        state["order"].append(("stamp", tid, dbname))
        return {"snapshotAt": AT, "sig": "3:99"}
    monkeypatch.setattr(rd.schema_catalog, "catalog_stamp", _stamp)

    def _fetch_all(sql, params=None):
        state["order"].append(("fetch_all", sql.split("FROM")[1].split()[0]))
        return []
    monkeypatch.setattr(rd.db, "fetch_all", _fetch_all)

    def _no_target(*a, **k):
        raise AssertionError("the stamp never reaches the target")
    monkeypatch.setattr(rd.psycopg, "connect", _no_target)
    monkeypatch.setattr(rd, "_ro_login", _no_target)
    return state


def test_the_stamp_route_answers_from_the_catalog(web):
    out = rd.db_schema_stamp("srv-alpha", "shop", CLAIMS)
    assert out == {"snapshotAt": AT, "sig": "3:99"}
    assert web["order"] == [("stamp", 7, "shop")]


def test_the_stamp_route_needs_the_grant(web):
    web["granted"] = None
    with pytest.raises(HTTPException) as e:
        rd.db_schema_stamp("srv-alpha", "shop", CLAIMS)
    assert (e.value.status_code, e.value.detail["code"]) == (404, "not_found")
    assert web["order"] == []


def test_schema_carries_the_stamp_read_before_the_tables(web):
    out = rd.db_schema("srv-alpha", "shop", CLAIMS)
    assert out["snapshotAt"] == AT and out["sig"] == "3:99"
    assert out["tables"] == [] and out["views"] == []
    assert web["order"][0] == ("stamp", 7, "shop"), web["order"]
    assert [o[0] for o in web["order"][1:]] == ["fetch_all", "fetch_all"]


def test_the_stamp_route_is_registered_under_api():
    paths = {getattr(r, "path", "") for r in rd.router.routes}
    assert "/api/connections/{conn}/databases/{dbname}/schema/stamp" in paths
