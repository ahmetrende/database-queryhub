"""Startup and shutdown, as behaviour rather than as a comment.

Two properties, both of which were quietly untrue at some point:

  1. An unreachable control DB at boot must start the process DEGRADED, not
     crash-loop it. The code caught init_pool() and said so in a comment — and
     then the very next line logged a banner containing base_url(), which reads
     bot_config through the pool that had just failed. That exception escaped
     startup and killed the process. An RDS failover during a deploy would
     crash-loop the web service, which is the precise outcome the comment
     claimed to prevent.

  2. Shutdown must run the drain. It is registered via `lifespan` now (FastAPI
     deprecated `@app.on_event` and warns on every boot); the halves are
     blocking and were previously run in a threadpool by the framework, so the
     lifespan keeps them there.
"""
import logging

import pytest
from starlette.testclient import TestClient

from dba_slack_bot import config as cfg
from dba_slack_bot import db, executor, lifecycle
from dba_slack_bot.web import app as web_app


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    logging.disable(logging.CRITICAL)
    from dba_slack_bot.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    yield
    logging.disable(logging.NOTSET)


def test_unreachable_control_db_starts_degraded(monkeypatch):
    """The whole failure, reproduced.

    Note what has to be stubbed and why: conftest's autouse `no_db_config`
    fixture makes `cfg.get_setting` return defaults without touching the DB, so
    by default a test CANNOT observe a failing config read — which is precisely
    the mechanism of this bug. Left as-is the test passes whether or not the fix
    is present (verified: it did). So put the failure back, on both halves of
    what a dead pool breaks: the pool itself, and every config read through it.
    """
    def boom(*a, **k):
        raise RuntimeError("control DB unreachable")

    monkeypatch.setattr(db, "init_pool", boom)
    monkeypatch.setattr(cfg, "get_setting", boom)
    # ...including the alias the auth module already imported.
    from dba_slack_bot.web import routes_auth
    monkeypatch.setattr(routes_auth.cfg, "get_setting", boom)

    # Reaching the body at all is the assertion: the app booted.
    with TestClient(web_app.create_app()) as c:
        # And it is honest about being unready, so a probe-driven rollout pulls
        # it from rotation rather than letting it serve errors.
        assert c.get("/readyz").status_code != 200


def test_liveness_still_answers_while_degraded(monkeypatch):
    """/healthz is liveness — it must NOT depend on the DB, or an outage makes
    the orchestrator kill a process that would have recovered."""
    monkeypatch.setattr(db, "init_pool",
                        lambda: (_ for _ in ()).throw(RuntimeError("down")))
    with TestClient(web_app.create_app()) as c:
        assert c.get("/healthz").status_code == 200


def test_shutdown_drains(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "init_pool", lambda: calls.append("init_pool"))
    monkeypatch.setattr(lifecycle, "begin_drain",
                        lambda: calls.append("begin_drain"))
    monkeypatch.setattr(executor, "shutdown",
                        lambda: calls.append("executor_shutdown"))

    with TestClient(web_app.create_app()):
        assert calls == ["init_pool"], "startup did not run, or ran too much"
    # Order matters: refuse new work first, then wait for in-flight.
    assert calls == ["init_pool", "begin_drain", "executor_shutdown"]


def test_no_deprecated_event_handlers():
    """`@app.on_event` is deprecated and slated for removal; it also warned on
    every single test that built an app. Keep it gone."""
    app = web_app.create_app()
    assert app.router.on_startup == []
    assert app.router.on_shutdown == []
    assert app.router.lifespan_context is not None
