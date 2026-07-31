"""A missing frontend build must be explicit, not a blank page.

The old chain fell back to the babel-in-browser prototype, which loads React
and Babel from unpkg.com and fonts from Google — all blocked by this app's own
CSP. So `git clone && install.sh` (which never built the bundle) produced a
completely blank page with console errors and a log line the browser user
never saw. The fallback is gone; the server now answers 503 with the exact
build command, and the API keeps serving.
"""
import logging
import tempfile

import pytest
from starlette.testclient import TestClient

from queryhub.web import app


@pytest.fixture
def client_without_build(monkeypatch):
    logging.disable(logging.CRITICAL)
    # An empty static dir stands in for "npm run build was never run".
    monkeypatch.setenv("QH_WEB_STATIC_DIR", tempfile.mkdtemp())
    from queryhub import db
    from queryhub.slack_app import notifications
    monkeypatch.setattr(db, "init_pool", lambda: None)
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    with TestClient(app.create_app()) as c:
        yield c


def test_root_says_the_frontend_is_not_built(client_without_build):
    r = client_without_build.get("/")
    assert r.status_code == 503
    assert "not built" in r.text
    # The page must carry the fix, not just the symptom.
    assert "npm run build" in r.text


def test_no_cdn_fallback_is_served(client_without_build):
    # Regression guard: the prototype pulled scripts from unpkg.com, which the
    # CSP blocks — serving it looked like success and rendered nothing.
    r = client_without_build.get("/")
    assert "unpkg.com" not in r.text


def test_api_and_health_still_work_without_a_build(client_without_build):
    assert client_without_build.get("/api/me").status_code == 401
    assert client_without_build.get("/healthz").status_code == 200
