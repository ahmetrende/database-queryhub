"""Startup warnings of the web process. Each one logs and never stops the start.

The Slack bot uses the pending-migration warning too, so that warning lives in
migration_state.py.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from urllib.parse import urlsplit

from . import routes_auth

log = logging.getLogger(__name__)


def plain_http_off_loopback(url: str) -> bool:
    """True when `url` uses http:// and its host is not a loopback address.

    Loopback is `localhost`, 127.0.0.0/8 and ::1. Traffic to those hosts stays
    on the machine, or goes through an encrypted SSH tunnel. So plain HTTP is
    safe there."""
    if not url.strip().lower().startswith("http://"):
        return False
    try:
        host = urlsplit(url.strip()).hostname or ""
    except ValueError:  # for example, an IPv6 host without its closing bracket
        return True
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:  # a DNS name
        return True


def warn_if_plain_http() -> None:
    """Log one CRITICAL line when the base URL sends sessions without TLS.

    The base URL is the origin that browsers use. Over http:// to another
    host, the session cookies and every response cross the network as plain
    text."""
    try:
        url = routes_auth.base_url()
    except Exception:
        log.debug("base URL check skipped: no base URL", exc_info=True)
        return
    if not plain_http_off_loopback(url):
        return
    # The same order as routes_auth.base_url(): the environment wins.
    setting = ("WEB_BASE_URL" if (os.environ.get("WEB_BASE_URL") or "").strip()
               else "bot_config web_base_url")
    log.critical("Session traffic is not encrypted: the base URL %s uses "
                 "http:// and its host is not loopback. Set %s to an "
                 "https:// URL.", url, setting)
