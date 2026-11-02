"""The Slack bot's health endpoint (health_server.py).

The bot has no HTTP port of its own, so a container platform could not see a
dead scheduler thread. These tests start the real server on a free port.
"""
import json
import urllib.error
import urllib.request

import pytest

from queryhub import health_server


def _get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def serve():
    servers = []

    def _start(checks):
        s = health_server.start(0, checks, host="127.0.0.1")
        servers.append(s)
        return s.server_port

    yield _start
    for s in servers:
        s.shutdown()
        s.server_close()


def test_all_checks_pass_gives_200(serve):
    port = serve({"scheduler": lambda: True, "auth_events": lambda: True})
    status, body = _get(port, "/healthz")
    assert status == 200
    assert json.loads(body) == {"status": "ok",
                                "checks": {"scheduler": True, "auth_events": True}}


def test_one_failing_check_gives_503_and_names_it(serve):
    port = serve({"scheduler": lambda: False, "auth_events": lambda: True})
    status, body = _get(port, "/healthz")
    assert status == 503
    data = json.loads(body)
    assert data["status"] == "unhealthy"
    assert data["checks"]["scheduler"] is False


def test_a_check_that_raises_counts_as_failed(serve):
    def boom():
        raise RuntimeError("no")

    port = serve({"scheduler": boom})
    status, body = _get(port, "/healthz")
    assert status == 503
    assert json.loads(body)["checks"] == {"scheduler": False}


def test_the_query_string_is_ignored(serve):
    port = serve({"scheduler": lambda: True})
    assert _get(port, "/healthz?probe=1")[0] == 200


def test_other_paths_are_404(serve):
    port = serve({"scheduler": lambda: True})
    assert _get(port, "/")[0] == 404
    assert _get(port, "/readyz")[0] == 404


@pytest.mark.parametrize("raw, expected", [
    ("", None),
    ("   ", None),
    ("8081", 8081),
    (" 9000 ", 9000),
    ("abc", None),
    ("0", None),
    ("70000", None),
])
def test_port_from_env(monkeypatch, raw, expected):
    monkeypatch.setenv("QH_HEALTH_PORT", raw)
    assert health_server.port_from_env() == expected


def test_no_env_means_no_port(monkeypatch):
    monkeypatch.delenv("QH_HEALTH_PORT", raising=False)
    assert health_server.port_from_env() is None
