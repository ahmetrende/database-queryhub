"""`python -m queryhub.web` — uvicorn runner for the web app.

Port comes from WEB_PORT (default 8080 — the port queryhub-web.service
owns and the operator's SSH tunnel forwards)."""
from __future__ import annotations

import os

import uvicorn

from .app import create_app


def main() -> None:
    # Same configuration as the bot process (LOG_LEVEL + LOG_FORMAT).
    from ..logging_setup import configure
    configure()
    # Optional TLS (dev: self-signed localhost cert — Slack OIDC requires
    # an https redirect URI, so the tunnel terminates TLS here). Passed as
    # explicit str|None kwargs (uvicorn accepts None = no TLS) rather than
    # **-unpacking an untyped dict.
    uvicorn.run(
        create_app(),
        host=os.environ.get("WEB_BIND", "0.0.0.0"),
        port=int(os.environ.get("WEB_PORT", "8080")),
        log_level="info",
        ssl_certfile=os.environ.get("WEB_SSL_CERTFILE") or None,
        ssl_keyfile=os.environ.get("WEB_SSL_KEYFILE") or None,
    )


if __name__ == "__main__":
    main()
