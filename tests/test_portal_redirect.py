"""The notice page for a person the own web UI refuses.

It says that QueryHub opens through the portal, links to the portal and sends
the person there after 5 seconds. The page is plain HTML with a meta refresh:
the content security policy allows no script, and the page must not depend on
the frontend bundle.
"""
import pytest
from starlette.testclient import TestClient

from queryhub.web import portal_redirect


@pytest.fixture
def setting(monkeypatch):
    state = {"value": ""}
    monkeypatch.setattr(portal_redirect.cfg, "get_setting",
                        lambda key, default=None: state["value"])
    return state


@pytest.mark.parametrize("raw", ["https://portal.example.test",
                                 "http://portal.example.test:8443/start",
                                 "  https://portal.example.test  "])
def test_an_http_or_https_address_with_a_host_is_the_portal(setting, raw):
    setting["value"] = raw
    assert portal_redirect.portal_url() == raw.strip()


@pytest.mark.parametrize("raw", ["", "   ", "javascript:alert(1)", "/relative/path",
                                 "portal.example.test", "ftp://portal.example.test",
                                 "https://", "data:text/html,x"])
def test_anything_else_reads_as_not_set(setting, raw):
    setting["value"] = raw
    assert portal_redirect.portal_url() == ""


def test_the_page_names_the_delay_links_the_portal_and_refreshes_to_it():
    html = portal_redirect.page("https://portal.example.test/")
    assert "QueryHub opens through the portal" in html
    assert "in 5 seconds" in html
    assert '<a href="https://portal.example.test/">Go to the portal now</a>' in html
    assert ('<meta http-equiv="refresh" content="5;url=https://portal.example.test/">'
            in html)


def test_the_page_has_no_script():
    """The policy allows none, and a script would be the skippable part."""
    assert "<script" not in portal_redirect.page("https://portal.example.test/").lower()


def test_an_address_cannot_break_out_of_the_attribute():
    html = portal_redirect.page('https://portal.example.test/?a="><script>x</script>')
    assert "<script>" not in html
    assert "&quot;&gt;&lt;script&gt;" in html


def test_without_an_address_the_page_has_no_link_and_no_refresh():
    html = portal_redirect.page("")
    assert "<a " not in html
    assert "http-equiv" not in html
    assert "Ask the DBA team for the portal address." in html


@pytest.fixture
def client(monkeypatch, setting):
    from queryhub.web import app as web_app
    return TestClient(web_app.create_app())


def test_the_route_serves_the_page_without_a_session(client, setting):
    setting["value"] = "https://portal.example.test/"
    r = client.get("/portal-redirect")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert "https://portal.example.test/" in r.text
    # The baseline policy still applies: no inline script anywhere.
    assert "script-src 'self';" in r.headers["content-security-policy"]


def test_a_changed_address_shows_at_once(client, setting):
    setting["value"] = "https://one.example.test/"
    assert "one.example.test" in client.get("/portal-redirect").text
    setting["value"] = "https://two.example.test/"
    assert "two.example.test" in client.get("/portal-redirect").text
