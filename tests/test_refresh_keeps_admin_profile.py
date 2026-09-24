"""A refresh keeps an admin's name and email, requester row or not.

Login admits an admin who has no `requesters` row, on their admin row. The
refresh read the profile from `requesters` alone, so every access token after
the first refresh named such a person with neither a name nor an email. No such
admin exists on the live system today (measured 2026-09-24); the gate admits
them, so the profile has to follow.
"""
import logging

import pytest
from starlette.testclient import TestClient

from queryhub import admins, db, requesters
from queryhub.web import app as web_app
from queryhub.web import deps, sessions


@pytest.fixture
def refresh(monkeypatch):
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "rotate_refresh", lambda tok: {
        "id": 7, "slack_user_id": "U0EXAMPLE001", "auth_provider": "slack",
        "avatar_url": None, "refresh_token": "next"})
    monkeypatch.setattr(deps, "slack_employment_ok", lambda uid: True)
    minted = []
    monkeypatch.setattr(sessions, "mint_access",
                        lambda claims, sid: minted.append(claims) or "access")

    def run(requester_row, admin_row, is_admin=True):
        monkeypatch.setattr(admins, "is_admin", lambda uid: is_admin)
        monkeypatch.setattr(requesters, "is_allowed", lambda uid: requester_row is not None)
        monkeypatch.setattr(requesters, "get", lambda uid: requester_row)
        monkeypatch.setattr(admins, "get", lambda uid: admin_row)
        with TestClient(web_app.create_app()) as c:
            c.cookies.set(deps.REFRESH_COOKIE, "tok")
            r = c.post("/api/auth/refresh")
        logging.disable(logging.NOTSET)
        assert r.status_code == 200, r.text
        return minted[-1]
    return run


def test_an_admin_without_a_requester_row_keeps_their_name(refresh):
    claims = refresh(None, {"slack_user_id": "U0EXAMPLE001", "name": "An Admin",
                            "email": "admin@example.com", "enabled": True})
    assert (claims["name"], claims["email"]) == ("An Admin", "admin@example.com")


def test_the_requester_row_still_wins_when_there_is_one(refresh):
    claims = refresh({"slack_user_id": "U0EXAMPLE001", "name": "As Requester",
                      "email": "r@example.com"},
                     {"slack_user_id": "U0EXAMPLE001", "name": "As Admin",
                      "email": "a@example.com"})
    assert claims["name"] == "As Requester"
