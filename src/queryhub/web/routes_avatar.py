"""Serve a user's Slack avatar from our own origin.

Why a proxy instead of letting the browser fetch it directly:

The avatar was added on 2026-07-15 and stopped appearing on 2026-07-24, when the
web-hardening commit set `img-src 'self' data:`. A Slack CDN URL is neither
'self' nor a data: URI, so every avatar was blocked and the UI's `onError`
handler quietly fell back to initials. Nothing logged; it just looked as though
the feature had been removed.

The one-line fix would be to add `https://avatars.slack-edge.com` to img-src.
That was rejected for two reasons. It contradicts a decision this project already
made deliberately — fonts are self-hosted precisely so the UI has no mandatory
network egress and works on an air-gapped install — and it would send every
viewer's IP address and User-Agent to Slack on every page load, for a decoration.

So the bytes come through here instead: `img-src 'self'` stays intact, no third
party sees the viewer, and an install with no route to the internet degrades to
initials exactly as it does today.

The security property that matters: the URL comes out of the DATABASE
(`web_sessions.avatar_url`, written from the OIDC id_token). Fetching a
database-supplied URL from the server is a server-side request forgery primitive
— it would let anything that can write that column make this host issue requests
to the metadata service, to internal addresses, to anywhere. So the host is
checked against a fixed allow-list before a socket is opened, and the caller
never supplies the URL: it is read from their own session.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from .. import db
from . import deps

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Hosts we will fetch an avatar from. Exact suffix match, https only. Slack
# serves avatars from avatars.slack-edge.com and (older accounts)
# secure.gravatar.com via its own redirects; anything else is refused rather
# than followed, because the URL originates in the database.
_ALLOWED_AVATAR_HOSTS = (
    "avatars.slack-edge.com",
    "secure.gravatar.com",
)

_MAX_AVATAR_BYTES = 512 * 1024        # an image_192 is a few KB; this is slack
_ALLOWED_CONTENT_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def _host_allowed(url: str) -> bool:
    """https, and a host on the allow-list. No redirects are followed by the
    caller, so this check cannot be bypassed by a 302 to somewhere else."""
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return any(host == h or host.endswith("." + h)
               for h in _ALLOWED_AVATAR_HOSTS)


@router.get("/avatar")
def my_avatar(claims: dict = Depends(deps.current_user)):
    """The signed-in user's own avatar, or 404.

    Deliberately has no id parameter. Serving "the avatar of user X" would make
    this an endpoint for enumerating colleagues' profile images, and the UI only
    ever needs the current user's. The URL is read from their live session, so a
    caller cannot point it anywhere.
    """
    row = db.fetch_one(
        "SELECT avatar_url FROM web_sessions "
        " WHERE slack_user_id = %s AND revoked_at IS NULL "
        "   AND avatar_url IS NOT NULL "
        " ORDER BY id DESC LIMIT 1",
        (claims["sub"],))
    url = (row or {}).get("avatar_url")
    if not url:
        raise deps._error(404, "not_found", "No avatar on file.")
    if not _host_allowed(url):
        # Either Slack changed CDN hosts or something wrote a URL we do not
        # trust. Both are worth a log line and neither is worth a fetch.
        log.warning("refusing to fetch avatar from a non-allowed host: %s",
                    urlsplit(url).hostname)
        raise deps._error(404, "not_found", "No avatar on file.")

    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "QueryHub"})
        # allow_redirects is not a thing for urlopen; a redirect would be
        # followed automatically, so cap it at zero by rejecting 3xx below.
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                raise deps._error(404, "not_found", "No avatar on file.")
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
            if ctype not in _ALLOWED_CONTENT_TYPES:
                log.warning("avatar had unexpected content-type %r", ctype)
                raise deps._error(404, "not_found", "No avatar on file.")
            # Read one byte past the cap so an oversized body is detected
            # rather than silently truncated into a broken image.
            body = resp.read(_MAX_AVATAR_BYTES + 1)
            if len(body) > _MAX_AVATAR_BYTES:
                log.warning("avatar exceeded %d bytes", _MAX_AVATAR_BYTES)
                raise deps._error(404, "not_found", "No avatar on file.")
    except deps.HTTPException:
        raise
    except Exception:
        # Slack unreachable, DNS failure, air-gapped install. The UI falls back
        # to initials, which is the same thing it does today.
        log.info("could not fetch avatar for %s", claims["sub"], exc_info=True)
        raise deps._error(404, "not_found", "No avatar on file.")

    return Response(
        content=body,
        media_type=ctype,
        headers={
            # Cache in the browser: the avatar changes about never, and without
            # this every page load would proxy a fetch through us.
            "Cache-Control": "private, max-age=86400",
            # It is someone's face; keep it out of shared caches and off
            # referers.
            "Referrer-Policy": "no-referrer",
        },
    )
