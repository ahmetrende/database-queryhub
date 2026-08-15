"""Baseline response security headers + cross-origin
(CSRF) refusal on state-changing requests."""
import logging

import pytest
from starlette.testclient import TestClient

from dba_slack_bot import db
from dba_slack_bot.web import app


@pytest.fixture
def client(monkeypatch):
    # These tests only exercise the middleware, which runs before any route
    # touches the DB — so stub the startup's DB pool + admin DM so the suite
    # needs no database or Slack (mirrors CI).
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    from dba_slack_bot.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    with TestClient(app.create_app()) as c:
        yield c


def test_security_headers_present_on_every_response(client):
    r = client.get("/api/me")  # 401, but headers still apply
    hdrs = {k.lower(): v for k, v in r.headers.items()}
    assert "content-security-policy" in hdrs
    assert "frame-ancestors 'none'" in hdrs["content-security-policy"]
    assert hdrs.get("x-content-type-options") == "nosniff"
    assert hdrs.get("x-frame-options") == "DENY"
    assert hdrs.get("referrer-policy") == "no-referrer"


def _csp_directives(header: str) -> dict[str, list[str]]:
    out = {}
    for part in header.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0].lower()] = tokens[1:]
    return out


def test_csp_forbids_inline_script(client):
    """The whole point of the policy. A CSP that keeps script-src
    'unsafe-inline' stops nothing an XSS cares about, so assert on the actual
    directive rather than merely on the header's presence."""
    csp = _csp_directives(client.get("/api/me").headers["content-security-policy"])
    assert "'unsafe-inline'" not in csp["script-src"]
    assert "'unsafe-eval'" not in csp["script-src"]
    assert csp["script-src"] == ["'self'"]
    # No external origin may supply script, objects, or frame us.
    assert csp["default-src"] == ["'self'"]
    assert csp["object-src"] == ["'none'"]
    assert csp["base-uri"] == ["'self'"]
    assert csp["form-action"] == ["'self'"]
    assert csp["frame-ancestors"] == ["'none'"]
    # Assets are bundled, so nothing needs a remote connect/font/img origin.
    assert csp["connect-src"] == ["'self'"]


def test_build_stamp_is_a_meta_tag_not_an_inline_script(tmp_path, monkeypatch):
    """The build version is stamped into the page as data. If it ever goes back
    to an injected <script>, script-src 'self' silently drops it and the version
    display breaks — so pin the shape."""
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    from dba_slack_bot.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)

    (tmp_path / "index.html").write_text(
        "<html><head><title>t</title></head><body></body></html>",
        encoding="utf-8")
    monkeypatch.setenv("QH_WEB_STATIC_DIR", str(tmp_path))
    from dba_slack_bot.web import build_info
    monkeypatch.setattr(build_info, "build", lambda: {
        # A quote in a branch name must not break out of the attribute.
        "version": "r999", "sha": "abc1234", "branch": 'we"ird', "date": "2026-07-25"})

    with TestClient(app.create_app()) as c:
        body = c.get("/").text
    assert '<meta name="qh-build"' in body
    assert "<script>" not in body
    assert "__QH_VERSION__" not in body
    # No raw quote reaches the markup — it can't terminate the attribute.
    assert 'we"ird' not in body

    # And the stamp still round-trips to the original object, which is what the
    # client-side reader does (html-unescape the attribute, JSON.parse it).
    import json as _json
    import re as _re
    from html import unescape
    content = _re.search(r'<meta name="qh-build" content="([^"]*)"', body).group(1)
    assert _json.loads(unescape(content))["branch"] == 'we"ird'


def test_cross_origin_state_change_refused(client):
    r = client.post("/api/queries",
                    headers={"origin": "https://evil.example.com"}, json={})
    assert r.status_code == 403


def test_same_origin_state_change_not_blocked_by_csrf_gate(client):
    # Same Origin host as Host -> the CSRF gate lets it through; it then
    # fails auth (401), NOT the 403 the cross-origin case gets.
    r = client.post("/api/queries",
                    headers={"origin": "http://testserver"}, json={})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# The Secure flag on session cookies. The documented install path produced an
# HTTPS deployment with non-Secure cookies: install.sh generates a certificate,
# sets WEB_SSL_CERTFILE and prints "open https://localhost:8080", and never
# touches web_cookie_secure — whose default was 'off'. Nothing enforced it and
# nothing warned. It is derived from the deployment now.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base_url,setting,expected", [
    # Derived (key unset or 'auto'):
    ("https://queryhub.example.com", "",     True),
    ("https://queryhub.example.com", "auto", True),
    ("http://localhost:8080",        "",     False),
    ("http://localhost:8080",        "auto", False),
    # Explicit wins in BOTH directions — an operator terminating TLS at a proxy
    # and reaching the app over plain HTTP internally can still say 'off'.
    ("https://queryhub.example.com", "off",  False),
    ("http://localhost:8080",        "on",   True),
    ("https://queryhub.example.com", "on",   True),
])
def test_cookie_secure_is_derived_from_the_deployment(
        monkeypatch, base_url, setting, expected):
    from dba_slack_bot.web import routes_auth

    def fake_get_setting(key, default=None):
        if key == "web_cookie_secure":
            return setting
        if key == "web_base_url":
            return base_url
        return default

    monkeypatch.setattr(routes_auth.cfg, "get_setting", fake_get_setting)
    monkeypatch.delenv("WEB_BASE_URL", raising=False)
    assert routes_auth._cookie_secure() is expected, (base_url, setting)
