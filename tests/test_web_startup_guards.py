"""Two startup guards, and /readyz with pending migrations.

  * A base URL with http:// to a host that is not loopback sends session
    cookies as plain text. The web app logs one CRITICAL line and starts.
  * Code that runs ahead of its migrations fails on the first query that
    needs a new table or column. Both services log one CRITICAL line at
    startup, and /readyz answers 503 until the runner applies the files.

The guards log and never stop a start: a log line must not decide whether
the service boots (tests/test_web_lifespan.py has the history).
"""
import logging

import pytest
from starlette.testclient import TestClient

from queryhub import db, migration_state
from queryhub.web import app as web_app
from queryhub.web import routes_auth, startup_checks


@pytest.fixture
def boot(monkeypatch):
    """Start the web app with no database and no Slack."""
    monkeypatch.setattr(db, "init_pool", lambda: None)
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    return lambda: TestClient(web_app.create_app())


@pytest.fixture
def pending_calls(monkeypatch):
    """Replace migration_state.pending with a stub that records its calls."""
    calls = []

    def install(verdict):
        def fake(shipped):
            calls.append(shipped)
            return verdict
        monkeypatch.setattr(migration_state, "pending", fake)
        return calls
    return install


def _critical(caplog, name):
    return [r for r in caplog.records if r.name == name and r.levelno == logging.CRITICAL]


# ---- /readyz ------------------------------------------------------------------

@pytest.fixture
def db_up(monkeypatch):
    monkeypatch.setattr(db, "fetch_one", lambda sql, params=None: {"ok": 1})


def test_readyz_is_unready_while_migrations_are_pending(boot, db_up, pending_calls):
    pending_calls(["144_a.sql", "145_b.sql"])
    with boot() as c:
        r = c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "unready", "detail": "2 migrations pending"}


@pytest.mark.parametrize("verdict", [[], None], ids=["none-pending", "unknown"])
def test_readyz_is_ready_when_nothing_is_pending_or_the_answer_is_unknown(
        boot, db_up, pending_calls, verdict):
    pending_calls(verdict)
    with boot() as c:
        r = c.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}


def test_readyz_reads_the_ledger_once_in_30_seconds(boot, db_up, pending_calls):
    calls = pending_calls([])
    with boot() as c:
        before = len(calls)          # the startup line made one call
        for _ in range(5):
            assert c.get("/readyz").status_code == 200
    assert len(calls) - before == 1


def test_readyz_reports_an_unreachable_database_first(boot, monkeypatch, pending_calls):
    def down(sql, params=None):
        raise RuntimeError("control DB unreachable")

    monkeypatch.setattr(db, "fetch_one", down)
    calls = pending_calls(["144_a.sql"])
    with boot() as c:
        before = len(calls)
        r = c.get("/readyz")
    assert r.status_code == 503
    assert r.json()["detail"] == "control database unreachable"
    assert len(calls) == before


# ---- startup lines --------------------------------------------------------------

def test_web_startup_logs_pending_migrations(boot, pending_calls, caplog):
    pending_calls(["144_a.sql"])
    caplog.set_level(logging.DEBUG)
    with boot():
        pass
    lines = _critical(caplog, migration_state.log.name)
    assert [r.getMessage() for r in lines] == [
        "1 migrations pending: 144_a.sql. Apply them with: "
        "python scripts/apply_migrations.py"]


def test_bot_startup_checks_migrations_right_after_it_opens_the_pool(monkeypatch):
    pytest.importorskip("slack_bolt")
    from queryhub import main as bot_main

    calls = []

    class _Stop(Exception):
        pass

    def stop():
        raise _Stop

    monkeypatch.setattr(bot_main, "_setup_logging", lambda: None)
    monkeypatch.setattr(bot_main.db, "init_pool", lambda: calls.append("init_pool"))
    monkeypatch.setattr(bot_main.targets, "log_tls_posture", lambda: calls.append("tls"))
    monkeypatch.setattr(migration_state, "warn_if_pending",
                        lambda: calls.append("migrations"))
    # The next startup step. Stop there: the rest needs Slack.
    monkeypatch.setattr(bot_main.executor, "reconcile_orphaned_executing", stop)
    with pytest.raises(_Stop):
        bot_main.main()
    assert calls == ["init_pool", "tls", "migrations"]


@pytest.mark.parametrize("url,warn", [
    ("http://localhost:8080", False),
    ("http://LOCALHOST:8080/", False),
    ("http://127.0.0.1:8080", False),
    ("http://127.9.8.7", False),          # all of 127.0.0.0/8
    ("http://[::1]:8080", False),
    ("https://queryhub.example.com", False),
    ("https://192.0.2.10:8080", False),   # 192.0.2.0/24: a documentation range
    ("", False),
    ("http://192.0.2.10:8080", True),
    ("HTTP://queryhub.example.com", True),
    ("http://0.0.0.0:8080", True),        # not loopback: it means "any address"
    ("http://localhost.example.com", True),
    ("http://[::1", True),                # not a valid URL: warn, never crash
])
def test_plain_http_off_loopback(url, warn):
    assert startup_checks.plain_http_off_loopback(url) is warn


def test_plain_http_to_a_remote_host_logs_one_critical_line(boot, monkeypatch, caplog):
    monkeypatch.setenv("WEB_BASE_URL", "http://queryhub.example.com")
    caplog.set_level(logging.DEBUG)
    with boot() as c:
        assert c.get("/healthz").status_code == 200
    lines = _critical(caplog, startup_checks.log.name)
    assert len(lines) == 1
    text = lines[0].getMessage()
    assert "not encrypted" in text and "http://queryhub.example.com" in text
    assert "Set WEB_BASE_URL to an https:// URL" in text


def test_the_line_names_bot_config_when_the_environment_has_no_base_url(
        monkeypatch, caplog):
    monkeypatch.delenv("WEB_BASE_URL", raising=False)

    def fake_get_setting(key, default=None):
        return "http://192.0.2.10:8080" if key == "web_base_url" else default

    monkeypatch.setattr(routes_auth.cfg, "get_setting", fake_get_setting)
    caplog.set_level(logging.DEBUG)
    startup_checks.warn_if_plain_http()
    lines = _critical(caplog, startup_checks.log.name)
    assert len(lines) == 1
    assert "Set bot_config web_base_url to an https:// URL" in lines[0].getMessage()


@pytest.mark.parametrize("url", ["http://localhost:8080", "https://queryhub.example.com"])
def test_a_safe_base_url_logs_nothing(boot, monkeypatch, caplog, url):
    monkeypatch.setenv("WEB_BASE_URL", url)
    caplog.set_level(logging.DEBUG)
    with boot():
        pass
    assert _critical(caplog, startup_checks.log.name) == []


def test_the_base_url_guard_never_stops_the_start(boot, monkeypatch, caplog):
    def broken():
        raise RuntimeError("control DB unreachable")

    monkeypatch.setattr(routes_auth, "base_url", broken)
    caplog.set_level(logging.DEBUG)
    startup_checks.warn_if_plain_http()      # no exception
    with boot() as c:                        # and the app starts
        assert c.get("/healthz").status_code == 200
    assert _critical(caplog, startup_checks.log.name) == []
