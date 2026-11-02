"""A small HTTP health endpoint for the Slack bot process.

The web process has /healthz and /readyz. The Slack bot has no HTTP port at
all: Socket Mode only connects out. A container platform then has nothing to
probe, and a bot whose scheduler thread died stays "running" while scheduled
requests never dispatch.

Set QH_HEALTH_PORT to start this endpoint. Without it nothing listens, so a
host install does not change.

    GET /healthz   200 when every check passes, else 503. The body is JSON
                   and names each check.

Only the standard library: the endpoint must work in every install profile.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

Check = Callable[[], bool]


def port_from_env() -> int | None:
    """The port in QH_HEALTH_PORT, or None when it is empty or not a port."""
    raw = (os.environ.get("QH_HEALTH_PORT") or "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        log.warning("QH_HEALTH_PORT=%r is not a number; the health endpoint "
                    "stays off", raw)
        return None
    if not 0 < port < 65536:
        log.warning("QH_HEALTH_PORT=%d is out of range; the health endpoint "
                    "stays off", port)
        return None
    return port


def _run_checks(checks: dict[str, Check]) -> dict[str, bool]:
    results = {}
    for name, check in checks.items():
        try:
            results[name] = bool(check())
        except Exception:
            log.exception("health check %s raised", name)
            results[name] = False
    return results


def start(port: int, checks: dict[str, Check],
          host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Serve /healthz on a daemon thread and return the server.

    `checks` maps a name to a callable that returns True when that part is
    healthy. Call `.shutdown()` on the result to stop it.
    """

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (the stdlib names it)
            if self.path.split("?", 1)[0] != "/healthz":
                self.send_error(404)
                return
            results = _run_checks(checks)
            ok = all(results.values())
            body = json.dumps({"status": "ok" if ok else "unhealthy",
                               "checks": results}).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            # A probe every few seconds would fill the log.
            return

    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="health-server",
                     daemon=True).start()
    log.info("health endpoint listening on %s:%d", host, server.server_port)
    return server
