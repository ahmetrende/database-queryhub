"""The page a refused person lands on while the own web UI is closed.

With `web_own_ui_super_admin_only` on, a person who is neither a super-admin
nor on `web_own_ui_allowed_users` cannot use this web UI. This page tells them
that QueryHub opens through the portal and sends them there after 5 seconds.
It also holds a link, so they can go at once.

The page is plain server-rendered HTML with a `<meta http-equiv="refresh">`.
It has no script, because the content security policy allows none, and it does
not depend on the frontend bundle. The refusal itself never rests on this page:
`deps.current_user` refuses every API call of a refused person.

The portal address is the `web_portal_url` key. An empty or invalid value
shows the notice without a link.
"""
from __future__ import annotations

import html
from urllib.parse import urlsplit

from .. import config as cfg

DELAY_SECONDS = 5
PATH = "/portal-redirect"


def portal_url() -> str:
    """The portal address, or "" when the key is empty or not an http(s) URL.

    Only an absolute http or https address with a host passes. A value such as
    `javascript:` or a relative path reads as "not set"."""
    raw = (cfg.get_setting("web_portal_url", "") or "").strip()
    parts = urlsplit(raw)
    if parts.scheme in ("http", "https") and parts.netloc:
        return raw
    return ""


def page(url: str) -> str:
    """The notice page for `url`. An empty `url` gives a page with no link and
    no redirect."""
    if url:
        safe = html.escape(url, quote=True)
        refresh = f'<meta http-equiv="refresh" content="{DELAY_SECONDS};url={safe}">'
        body = (f"<p>QueryHub sends you to the portal in {DELAY_SECONDS} seconds.</p>"
                f'<p><a href="{safe}">Go to the portal now</a></p>')
    else:
        refresh = ""
        body = "<p>Ask the DBA team for the portal address.</p>"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{refresh}<title>QueryHub</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:0;display:flex;"
        "min-height:100vh;align-items:center;justify-content:center;"
        "background:#f6f7f8;color:#1c2024}main{max-width:26rem;padding:2rem;"
        "text-align:center}h1{font-size:1.25rem;margin:0 0 1rem}"
        "a{color:#0b6e4f;font-weight:600}</style></head><body><main>"
        "<h1>QueryHub opens through the portal</h1>"
        f"{body}</main></body></html>")
