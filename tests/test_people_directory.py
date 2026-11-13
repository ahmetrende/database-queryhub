"""GET /admin/people/directory: find a person by Slack id, email or name (no live DB).

A new person used to need a Slack id. The picker now finds people in QueryHub
and in the Slack directory by any of the three. Nothing is written here.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub.web import routes_admin as ra

ADMIN = {"sub": "U0EXAMPLE001"}
HERE = [
    {"principal": "U0EXAMPLE002", "name": "Jordan Ray", "email": "jordan.ray@example.com",
     "enabled": True, "is_admin": False},
    {"principal": "U0EXAMPLE003", "name": "Ada Test", "email": None,
     "enabled": True, "is_admin": True},
]
SLACK = [
    {"principal": "U0EXAMPLE004", "name": "Zo\u00eb \u00c5ngstr\u00f6m", "email": "z.angstrom@example.com"},
    {"principal": "U0EXAMPLE002", "name": "Jordan Ray", "email": "jordan.ray@example.com"},
    {"principal": "U0EXAMPLE005", "name": "Jordan Blake", "email": "jordan.blake@example.com"},
]


@pytest.fixture
def world(monkeypatch):
    st = {"slack_calls": [], "email_calls": [], "slack_error": None}
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, area: None)
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: [dict(r) for r in HERE])
    # cfg.ENV is frozen, and slack_enabled is a property: swap the object.
    monkeypatch.setattr(ra.cfg, "ENV", SimpleNamespace(slack_enabled=True, slack_bot_token="x"))

    def directory():
        st["slack_calls"].append(1)
        if st["slack_error"]:
            raise st["slack_error"]
        return [dict(p) for p in SLACK]

    def by_email(email):
        st["email_calls"].append(email)
        return next((dict(p) for p in SLACK if p["email"] == email.lower()), None)

    monkeypatch.setattr(ra, "_slack_directory", directory)
    monkeypatch.setattr(ra, "_slack_by_email", by_email)
    return st


def _ids(r):
    return [p["principal"] for p in r["people"]]


def test_a_name_finds_people_in_queryhub_first_then_in_slack(world):
    r = ra.admin_people_directory(q="jordan", claims=ADMIN)
    assert _ids(r) == ["U0EXAMPLE002", "U0EXAMPLE005"]
    assert r["people"][0]["known"] is True
    assert r["people"][1] == {"principal": "U0EXAMPLE005", "known": False, "name": "Jordan Blake",
                              "email": "jordan.blake@example.com", "enabled": False, "admin": False}
    assert r["directory"] == "ok" and r["more"] is False


def test_a_name_without_accents_finds_the_accented_name(world):
    r = ra.admin_people_directory(q="zoe angstrom", claims=ADMIN)
    assert _ids(r) == ["U0EXAMPLE004"]
    assert r["people"][0]["known"] is False


def test_the_capital_dotted_i_folds_to_i():
    assert ra._fold("\u0130LKER I\u0131k") == "ilker iik"
    assert ra._name_matches("ilker", "\u0130lker Test", None)


def test_every_word_must_match(world):
    assert _ids(ra.admin_people_directory(q="jordan zed", claims=ADMIN)) == []


def test_an_email_asks_slack_for_that_address_only(world):
    r = ra.admin_people_directory(q="Z.Angstrom@example.com", claims=ADMIN)
    assert _ids(r) == ["U0EXAMPLE004"]
    assert world["email_calls"] == ["Z.Angstrom@example.com"]
    assert world["slack_calls"] == []


def test_an_email_of_someone_in_queryhub_is_known(world):
    r = ra.admin_people_directory(q="jordan.ray@example.com", claims=ADMIN)
    assert _ids(r) == ["U0EXAMPLE002"] and r["people"][0]["known"] is True


def test_a_slack_id_resolves_as_before(world, monkeypatch):
    monkeypatch.setattr(ra, "_resolve_principal", lambda pid: {
        "principal": pid, "known": False, "name": "Jordan Blake",
        "email": "jordan.blake@example.com", "enabled": False, "admin": False})
    r = ra.admin_people_directory(q="U0EXAMPLE004", claims=ADMIN)
    assert _ids(r) == ["U0EXAMPLE004"]
    monkeypatch.setattr(ra, "_resolve_principal", lambda pid: {
        "principal": pid, "known": False, "name": None, "email": None,
        "enabled": False, "admin": False})
    assert ra.admin_people_directory(q="U0EXAMPLE009", claims=ADMIN)["people"] == []


def test_slack_down_still_lists_queryhub_people(world):
    world["slack_error"] = RuntimeError("slack is down")
    r = ra.admin_people_directory(q="jordan", claims=ADMIN)
    assert _ids(r) == ["U0EXAMPLE002"] and r["directory"] == "unavailable"


def test_no_slack_lists_queryhub_people_only(world, monkeypatch):
    monkeypatch.setattr(ra.cfg, "ENV", SimpleNamespace(slack_enabled=False, slack_bot_token=""))
    r = ra.admin_people_directory(q="ada", claims=ADMIN)
    assert _ids(r) == ["U0EXAMPLE003"] and r["directory"] == "off"
    assert world["slack_calls"] == []


def test_a_short_query_is_refused(world):
    with pytest.raises(HTTPException) as e:
        ra.admin_people_directory(q=" j ", claims=ADMIN)
    assert e.value.status_code == 400


def test_the_list_is_capped(world, monkeypatch):
    many = [{"principal": f"U0EXAMPLE{100 + i}", "name": f"Jordan {i:02d}", "email": None}
            for i in range(30)]
    monkeypatch.setattr(ra, "_slack_directory", lambda: many)
    r = ra.admin_people_directory(q="jordan", claims=ADMIN)
    assert len(r["people"]) == ra._DIRECTORY_LIMIT and r["more"] is True


def test_the_slack_directory_skips_bots_apps_and_deactivated_people(monkeypatch):
    pages = [
        {"members": [
            {"id": "U0EXAMPLE006", "real_name": "Real Person", "profile": {"email": "real@example.com"}},
            {"id": "U0EXAMPLE007", "is_bot": True, "real_name": "A Bot", "profile": {}},
            {"id": "U0EXAMPLE008", "deleted": True, "real_name": "Gone", "profile": {}},
            {"id": ra._SLACK_SYSTEM_USER, "real_name": "Slack system user", "profile": {}},
        ], "response_metadata": {"next_cursor": "c2"}},
        {"members": [
            {"id": "U0EXAMPLE009", "real_name": "", "profile": {"display_name": "Shown Name"}},
        ], "response_metadata": {"next_cursor": ""}},
    ]
    calls = []

    class _Client:
        def users_list(self, **kw):
            calls.append(kw)
            return pages[len(calls) - 1]

    monkeypatch.setattr(ra, "_directory_client", lambda: _Client())
    monkeypatch.setattr(ra, "_directory_cache", {"at": 0.0, "people": []})
    got = ra._slack_directory()
    assert [p["principal"] for p in got] == ["U0EXAMPLE006", "U0EXAMPLE009"]
    assert got[1]["name"] == "Shown Name"
    assert calls == [{"limit": 200}, {"limit": 200, "cursor": "c2"}]
    # A second call inside the TTL reads the cache.
    assert ra._slack_directory() is got and len(calls) == 2
