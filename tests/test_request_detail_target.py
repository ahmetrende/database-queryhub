"""The connection and database that GET /api/queries/<id> sends for a tab.

A tab that opens a request binds to the connection in the answer. Only a
connection that the person can use now goes out. A retired target, a deleted
one or one outside the person's grants gives none, so the tab opens with no
connection, as Welcome opens such a history row.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from queryhub.web import routes_data, routes_queries

ME = "U0EXAMPLE001"
LIVE = SimpleNamespace(id=7, alias="orders", enabled=True)
RETIRED = SimpleNamespace(id=8, alias="old-orders", enabled=False)


@pytest.fixture
def world(monkeypatch):
    st = {"targets": {7: LIVE, 8: RETIRED}, "granted": {7, 8}}
    monkeypatch.setattr(routes_data.targets, "get", lambda tid: st["targets"].get(int(tid)))
    monkeypatch.setattr(routes_data.teams, "effective_grant_for_user",
                        lambda uid, tid: {"mode": "ro"} if tid in st["granted"] else None)
    return st


def _row(tid):
    return {"target_server_id": tid, "database_name": "shop"}


def test_a_connection_that_the_person_can_use_goes_out(world):
    assert routes_queries._usable_target(_row(7), ME) == ("orders", "shop")


@pytest.mark.parametrize("tid,why", [(8, "retired"), (9, "gone"), (None, "none")])
def test_a_connection_that_the_person_cannot_use_does_not(world, tid, why):
    assert routes_queries._usable_target(_row(tid), ME) == (None, None), why


def test_a_connection_outside_the_grants_does_not(world):
    world["granted"] = set()
    assert routes_queries._usable_target(_row(7), ME) == (None, None)


def test_the_detail_route_sends_the_usable_target():
    import inspect
    src = inspect.getsource(routes_queries.query_status)
    assert "_usable_target(row, claims[\"sub\"])" in src
    assert "_alias_of(row[\"target_server_id\"])" not in src
