"""Saved workspaces (/sessions): what a tab keeps across a save and a load.

The portal names a new tab after its database and renames it when the tab
moves to another database. A saved workspace must keep that, or a reloaded tab
keeps the name it showed at save time.
"""
import json

from queryhub.web import mapping, routes_data

CLAIMS = {"sub": "U0EXAMPLE001"}


def _stored_tabs(monkeypatch, tabs):
    seen = []
    monkeypatch.setattr(routes_data.deps, "require_whitelisted", lambda c: None)
    monkeypatch.setattr(routes_data.db, "fetch_one", lambda sql, p: seen.append(p) or None)
    monkeypatch.setattr(routes_data.mapping, "session_entry", lambda r: r)
    routes_data.sessions_upsert(routes_data.SessionIn(name="w", tabs=tabs), CLAIMS)
    return json.loads(seen[0][2])


def test_an_auto_named_tab_is_stored_with_its_flag(monkeypatch):
    stored = _stored_tabs(monkeypatch, [
        {"name": "shop", "autoName": True, "sql": "SELECT 1", "conn": "c1", "db": "shop"},
        {"name": "Monthly report", "sql": "SELECT 2", "conn": "c1", "db": "shop"},
    ])
    assert stored[0] == {"name": "shop", "autoName": True, "sql": "SELECT 1",
                         "connectionId": "c1", "databaseId": "shop"}
    assert "autoName" not in stored[1]


def test_only_a_true_boolean_marks_a_tab_auto_named(monkeypatch):
    stored = _stored_tabs(monkeypatch, [
        {"name": "a", "autoName": "true"},
        {"name": "b", "autoName": 1},
        {"name": "c", "autoName": False},
    ])
    assert all("autoName" not in t for t in stored)


def test_an_auto_named_tab_with_no_name_still_gets_a_name(monkeypatch):
    stored = _stored_tabs(monkeypatch, [{"name": "", "autoName": True, "sql": "SELECT 1"}])
    assert stored[0]["name"] == "Untitled query" and stored[0]["autoName"] is True


def test_a_loaded_tab_says_whether_it_is_auto_named():
    row = {"id": 4, "name": "w", "updated_at": None, "tabs": [
        {"name": "shop", "autoName": True, "sql": "SELECT 1", "connectionId": "c1", "databaseId": "shop"},
        {"name": "Monthly report", "sql": "SELECT 2", "connectionId": "c1", "databaseId": "shop"},
    ]}
    tabs = mapping.session_entry(row)["tabs"]
    assert tabs[0] == {"name": "shop", "sql": "SELECT 1", "conn": "c1", "db": "shop", "autoName": True}
    assert tabs[1]["autoName"] is False
