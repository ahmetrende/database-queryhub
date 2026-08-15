"""The avatar proxy, and the SSRF guard that is the real reason it needs tests.

The avatar was added on 2026-07-15 and stopped appearing on 2026-07-24, when the
web-hardening commit set `img-src 'self' data:`. A Slack CDN URL is neither
'self' nor a data: URI, so the browser blocked every avatar and the UI's
`onError` fell back to initials. Nothing logged, nothing failed — it read as the
feature having been deleted, which is how it was reported.

Adding `avatars.slack-edge.com` to the CSP would have been one line. It was
rejected: this project self-hosts its fonts specifically so the UI has no
mandatory network egress, and a CDN avatar sends every viewer's IP and
User-Agent to Slack on each page load. Proxying keeps `img-src 'self'` intact.

That choice creates the hazard these tests exist for. The URL comes from the
DATABASE, and fetching a database-supplied URL server-side is a server-side
request forgery primitive: anything able to write that column could make this
host request the cloud metadata service, an internal address, or a file:// path.
So the host is checked against a fixed allow-list BEFORE a socket is opened, and
the assertion below is not merely "it 404s" but "it never fetched".
"""
import logging

import pytest
from starlette.testclient import TestClient

from dba_slack_bot import admins, db, requesters
from dba_slack_bot.web import app as web_app
from dba_slack_bot.web import routes_avatar, sessions


class FakeResponse:
    status = 200
    headers = {"content-type": "image/png"}

    def __init__(self, body=b"\x89PNG\r\n\x1a\n" + b"0" * 50):
        self._body = body

    def read(self, n):
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def client(monkeypatch):
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    from dba_slack_bot.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "verify_access",
                        lambda t: {"sub": "U0X", "sid": "s", "provider": "slack"}
                        if t == "good" else None)
    monkeypatch.setattr(sessions, "session_alive", lambda s, principal=None: True)
    monkeypatch.setattr(admins, "is_admin", lambda u: False)
    monkeypatch.setattr(requesters, "is_allowed", lambda u: True)
    with TestClient(web_app.create_app()) as c:
        c.cookies.set("qh_session", "good")
        yield c


@pytest.fixture
def fetches(monkeypatch):
    """Counts outbound fetches. The SSRF assertions read this, not just the
    status code — a 404 produced AFTER a request to the metadata service would
    still have made the request."""
    import urllib.request
    calls = []

    def spy(req, timeout=None):
        calls.append(getattr(req, "full_url", req))
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    return calls


def _avatar_is(monkeypatch, url):
    monkeypatch.setattr(routes_avatar.db, "fetch_one",
                        lambda sql, p=None: {"avatar_url": url} if url else None)


# ------------------------------------------------------------- happy path


def test_an_allowed_host_is_proxied_with_image_bytes(client, fetches, monkeypatch):
    _avatar_is(monkeypatch, "https://avatars.slack-edge.com/2026/abc_192.png")
    r = client.get("/api/avatar")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    assert len(fetches) == 1


def test_the_response_is_cached_and_kept_private(client, fetches, monkeypatch):
    """Without caching, every page load proxies a fetch through us. And it is
    someone's face, so it must not land in a shared cache."""
    _avatar_is(monkeypatch, "https://avatars.slack-edge.com/a.png")
    r = client.get("/api/avatar")
    cc = r.headers.get("cache-control", "")
    assert "private" in cc and "max-age=" in cc


def test_no_avatar_on_file_is_a_404(client, fetches, monkeypatch):
    _avatar_is(monkeypatch, None)
    assert client.get("/api/avatar").status_code == 404
    assert fetches == []


def test_an_anonymous_caller_gets_nothing(client, monkeypatch):
    _avatar_is(monkeypatch, "https://avatars.slack-edge.com/a.png")
    client.cookies.clear()
    assert client.get("/api/avatar").status_code == 401


# ------------------------------------------------------------------- SSRF


@pytest.mark.parametrize("url,why", [
    ("http://169.254.169.254/latest/meta-data/iam/",
     "cloud metadata service — the classic SSRF target"),
    ("http://127.0.0.1:8080/api/admin/config", "our own admin API"),
    ("http://192.0.2.5:5432/", "an internal address (RFC 5737 range)"),
    ("https://evil.example.com/a.png", "an arbitrary host"),
    ("https://avatars.slack-edge.com.evil.test/a.png",
     "suffix confusion — the allowed host as a PREFIX of the real one"),
    ("https://notavatars.slack-edge.com/a.png",
     "the allowed host as a SUFFIX without a dot boundary"),
    ("file:///etc/passwd", "not even http"),
    ("http://avatars.slack-edge.com/a.png", "right host, but plain http"),
])
def test_a_url_outside_the_allow_list_is_never_fetched(client, fetches,
                                                      monkeypatch, url, why):
    _avatar_is(monkeypatch, url)
    assert client.get("/api/avatar").status_code == 404, why
    assert fetches == [], f"opened a socket to {url} ({why})"


def test_host_matching_requires_a_dot_boundary():
    """Unit-level, because the parametrised cases above all funnel through it and
    an off-by-one here is the whole guard."""
    ok = routes_avatar._host_allowed
    assert ok("https://avatars.slack-edge.com/a.png")
    assert ok("https://cdn.avatars.slack-edge.com/a.png")     # real subdomain
    assert not ok("https://avatars.slack-edge.com.evil.test/a.png")
    assert not ok("https://xavatars.slack-edge.com/a.png")
    assert not ok("http://avatars.slack-edge.com/a.png")      # scheme
    assert not ok("https:///a.png")                           # no host
    assert not ok("not a url at all")


# ------------------------------------------------- what comes back matters too


def test_a_non_image_content_type_is_refused(client, monkeypatch):
    """The allow-list bounds WHERE we fetch from; this bounds what we will hand
    to a browser as an image."""
    import urllib.request

    class Html(FakeResponse):
        headers = {"content-type": "text/html"}

    monkeypatch.setattr(urllib.request, "urlopen", lambda r, timeout=None: Html())
    _avatar_is(monkeypatch, "https://avatars.slack-edge.com/a.png")
    assert client.get("/api/avatar").status_code == 404


def test_an_oversized_body_is_refused_rather_than_truncated(client, monkeypatch):
    """Truncating would hand the browser a corrupt image and look like a render
    bug; refusing falls back to initials, which is honest."""
    import urllib.request
    big = b"\x89PNG" + b"0" * (routes_avatar._MAX_AVATAR_BYTES + 10)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda r, timeout=None: FakeResponse(big))
    _avatar_is(monkeypatch, "https://avatars.slack-edge.com/a.png")
    assert client.get("/api/avatar").status_code == 404


def test_slack_being_unreachable_degrades_to_no_avatar(client, monkeypatch):
    """An air-gapped install, a DNS failure, Slack down. The UI shows initials,
    exactly as it does today — a decoration must not break the page."""
    import urllib.request

    def boom(*a, **k):
        raise OSError("Name or service not known")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    _avatar_is(monkeypatch, "https://avatars.slack-edge.com/a.png")
    assert client.get("/api/avatar").status_code == 404


# ------------------------------------------------------ the CSP stays closed


def test_the_csp_still_allows_no_external_images(client, monkeypatch):
    """The whole point of proxying. If a later change adds a CDN host here, the
    proxy has become pointless and every viewer's IP goes to that CDN again."""
    _avatar_is(monkeypatch, None)
    csp = client.get("/api/avatar").headers["content-security-policy"]
    img = [d for d in csp.split(";") if d.strip().startswith("img-src")][0]
    assert img.split() == ["img-src", "'self'", "data:"], img


def test_me_hands_the_ui_our_own_url_not_a_cdn_one():
    """/api/me used to return the slack-edge URL, which the CSP then blocked.
    It must point at the proxy instead — asserted on the source because building
    a full /me response needs the whole admin/grant stack."""
    import inspect
    from dba_slack_bot.web import app as app_mod
    src = inspect.getsource(app_mod.create_app)
    assert '"avatar": "/api/avatar"' in src
    assert 'claims.get("avatar")' in src, "still gated on having one on file"
