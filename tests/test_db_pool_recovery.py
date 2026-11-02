"""A control DB that is down at boot must not leave a dead pool behind.

psycopg_pool closes the pool when `wait()` times out. init_pool() used to keep
that closed pool in `_pool`. So connection() never opened a new one, pool_ready()
said True, and every query raised "the pool is already closed" until the
process restarted. The web service stayed unready after a host start while the
control DB was unreachable for the first minute.

These tests use a fake pool that closes itself on a wait timeout, like the real
one. The unit-test fixture in conftest replaces `db.init_pool`, so the tests put
the real function back.
"""
import contextlib
import time

import pytest
from psycopg_pool import PoolClosed, PoolTimeout
from starlette.testclient import TestClient

from queryhub import db, migration_state
from queryhub.web import app as web_app

REAL_INIT_POOL = db.init_pool


class FakeCursor:
    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return {"ok": 1}


class FakeConn:
    @contextlib.contextmanager
    def cursor(self):
        yield FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass


class FakePool:
    """Stands in for ConnectionPool. `reachable` is the state of the DB."""
    reachable = False
    made: list = []

    def __init__(self, **kwargs):
        self.closed = False
        self.wait_timeouts = []
        FakePool.made.append(self)

    def wait(self, timeout=30.0):
        self.wait_timeouts.append(timeout)
        if not FakePool.reachable:
            self.close()  # what psycopg_pool does when the wait times out
            raise PoolTimeout(f"pool initialization incomplete after {timeout} sec")

    def close(self):
        self.closed = True

    @contextlib.contextmanager
    def connection(self):
        if self.closed:
            raise PoolClosed("the pool 'pool-1' is already closed")
        yield FakeConn()


@pytest.fixture
def pool(monkeypatch):
    FakePool.reachable = False
    FakePool.made = []
    monkeypatch.setattr(db, "ConnectionPool", FakePool)
    monkeypatch.setattr(db, "init_pool", REAL_INIT_POOL)
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db, "_connect_failed_at", None, raising=False)
    return FakePool


def _cooldown_over():
    """Let the retry cooldown pass without sleeping."""
    db._connect_failed_at = time.monotonic() - db.RETRY_COOLDOWN_SEC - 1


def test_a_failed_first_connect_leaves_no_pool(pool):
    with pytest.raises(PoolTimeout):
        db.init_pool()
    assert db.pool_ready() is False
    assert db._pool is None


def test_a_query_connects_after_the_db_returns(pool):
    with pytest.raises(PoolTimeout):
        db.init_pool()
    pool.reachable = True
    _cooldown_over()
    assert db.fetch_one("SELECT 1") == {"ok": 1}
    assert db.pool_ready() is True


def test_a_transaction_connects_after_the_db_returns(pool):
    with pytest.raises(PoolTimeout):
        db.init_pool()
    pool.reachable = True
    _cooldown_over()
    with db.transaction() as cur:
        cur.execute("SELECT 1")
    assert db.pool_ready() is True


def test_calls_inside_the_cooldown_fail_at_once(pool):
    with pytest.raises(PoolTimeout):
        db.init_pool()
    for _ in range(3):
        with pytest.raises(PoolTimeout):
            db.fetch_one("SELECT 1")
    assert len(pool.made) == 1, "each query built a pool of its own"


def test_the_retry_waits_less_than_the_first_attempt(pool):
    with pytest.raises(PoolTimeout):
        db.init_pool()
    _cooldown_over()
    with pytest.raises(PoolTimeout):
        db.fetch_one("SELECT 1")
    assert [p.wait_timeouts for p in pool.made] == [[30.0], [db.RETRY_WAIT_SEC]]


def test_a_first_lazy_connect_waits_the_full_time(pool):
    pool.reachable = True
    assert db.fetch_one("SELECT 1") == {"ok": 1}
    assert [p.wait_timeouts for p in pool.made] == [[30.0]]


def test_close_pool_forgets_an_earlier_failure(pool):
    with pytest.raises(PoolTimeout):
        db.init_pool()
    db.close_pool()
    pool.reachable = True
    assert db.fetch_one("SELECT 1") == {"ok": 1}


def test_a_pool_that_is_open_is_left_alone(pool):
    pool.reachable = True
    db.init_pool()
    db.init_pool()
    assert db.fetch_one("SELECT 1") == {"ok": 1}
    assert len(pool.made) == 1


def test_the_web_app_becomes_ready_without_a_restart(pool, monkeypatch):
    """The whole failure: start with the DB down, bring the DB back, and ask
    /readyz again. It used to answer 503 until the process restarted."""
    monkeypatch.setattr(migration_state, "pending", lambda shipped: [])
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    with TestClient(web_app.create_app()) as c:
        assert c.get("/readyz").status_code == 503
        assert c.get("/healthz").status_code == 200
        pool.reachable = True
        _cooldown_over()
        assert c.get("/readyz").status_code == 200
