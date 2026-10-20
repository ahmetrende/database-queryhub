"""A database that refuses the read-only login leaves the listing and the catalog.

The hourly refresh used to list every database on a server and then fail to open
the ones that refuse the read-only login. Each of them cost one failed
connection per hour. Nothing on screen said so, because a run in which at least
one database works counts as healthy. The old rows of such a database stayed in
the sidebar for good. The listing now names only the databases that the login
may open, so the prune that removes a dropped database removes these too.

These tests pin what the code does with the server's answer. Whether the answer
is the right one is a question for a real server, and
tests/test_integration_catalog_connect.py asks it.
"""
from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException

from queryhub import schema_catalog as sc
from queryhub import targets
from queryhub.web import routes_admin


def _target(alias="example-rds"):
    return targets.TargetServer(
        id=7, alias=alias, host="example.internal", port=5432,
        default_database="postgres", username="u", enabled=True, notes=None,
        engine="postgres", engine_config=None)


class _Cur:
    """Answers the listing query with its own rows."""

    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        pass

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self.cur


def _server_lists(monkeypatch, rows):
    monkeypatch.setattr(sc, "_connect", lambda target, pw, database: _Conn(_Cur(rows)))


# ---- the listing ----------------------------------------------------------

def test_a_database_that_refuses_the_login_is_left_out_and_logged(monkeypatch, caplog):
    _server_lists(monkeypatch, [("app_a", True), ("closed_db", False), ("app_b", True)])
    with caplog.at_level(logging.INFO, logger=sc.log.name):
        listed = sc.list_target_databases(_target(), "pw")
    assert listed == ["app_a", "app_b"]
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "closed_db" in line and "example-rds" in line and "CONNECT" in line
    assert "app_a" not in line, "only the databases that were left out are named"


def test_a_server_where_every_database_opens_logs_nothing(monkeypatch, caplog):
    _server_lists(monkeypatch, [("app_a", True), ("app_b", True)])
    with caplog.at_level(logging.INFO, logger=sc.log.name):
        assert sc.list_target_databases(_target(), "pw") == ["app_a", "app_b"]
    assert caplog.records == []


# ---- the catalog follows the listing --------------------------------------

def test_a_database_that_closes_to_the_login_leaves_the_catalog(monkeypatch):
    """The listing no longer names it, so the plan removes its rows, as it does
    when someone drops a database."""
    _server_lists(monkeypatch, [("app_a", True), ("closed_db", False), ("app_b", True)])
    listed = sc.list_target_databases(_target(), "pw")
    assert sc.plan_prune(["app_a", "app_b", "closed_db"], listed) == ["closed_db"]


def test_a_login_that_can_open_nothing_removes_nothing(monkeypatch):
    """A role that lost CONNECT everywhere is a permission problem. It is not a
    server with no database, so the guard keeps every row."""
    _server_lists(monkeypatch, [("app_a", False), ("app_b", False)])
    listed = sc.list_target_databases(_target(), "pw")
    assert listed == []
    assert sc.plan_prune(["app_a", "app_b"], listed) == []


# ---- the admin refresh button ---------------------------------------------

def test_a_refresh_that_names_a_database_the_login_cannot_open_says_so(monkeypatch):
    monkeypatch.setattr(routes_admin.admin, "require_admin", lambda c, s: "U1")
    monkeypatch.setattr(routes_admin.targets, "by_alias", lambda alias: _target(alias))
    monkeypatch.setattr(routes_admin.targets, "get_password", lambda tid: "pw")
    monkeypatch.setattr(routes_admin.schema_catalog, "list_target_databases",
                        lambda t, pw: ["app_a"])
    with pytest.raises(HTTPException) as err:
        routes_admin.admin_schema_refresh("example-rds", database="closed_db",
                                          claims={"sub": "U1"})
    assert err.value.status_code == 404
    assert "closed_db" in err.value.detail["message"]
    assert "can open" in err.value.detail["message"]
