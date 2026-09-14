"""Shared pytest fixtures.

The package's `config` module runs `EnvConfig.from_env()` at import time
and `cfg.get_setting()` reads `bot_config` from the DB. Tests run with
neither a real env nor a DB, so we (1) seed dummy env vars BEFORE any
package import (module top-level, so it happens during collection), and
(2) autouse-patch `get_setting` to return defaults without touching the
DB. Tests that need a specific config value monkeypatch it themselves.
"""
import atexit
import logging
import os
import tempfile

from cryptography.fernet import Fernet

# Seed required env vars before importing the package (config.from_env()
# runs at import time and would raise on missing vars).
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("BOT_DB_HOST", "localhost")
os.environ.setdefault("BOT_DB_PORT", "5432")
os.environ.setdefault("BOT_DB_NAME", "test")
os.environ.setdefault("BOT_DB_USER", "test")
os.environ.setdefault("BOT_DB_PASSWORD", "test")

# The master key is FORCED, not `setdefault`. Running anything on the host
# means sourcing the bot's env file first, and that file points
# MASTER_KEY_PATH at the production key — so `setdefault` quietly handed the
# live secret to the test suite on every local run, while the same tests blew
# up in CI where no key exists. A per-session throwaway key makes the suite
# behave identically in both places and keeps the real key out of it.
_key_fd, _key_path = tempfile.mkstemp(prefix="queryhub-test-", suffix=".key")
os.write(_key_fd, Fernet.generate_key())
os.close(_key_fd)
os.chmod(_key_path, 0o600)          # crypto refuses a world-readable key file
os.environ["MASTER_KEY_PATH"] = _key_path


@atexit.register
def _remove_test_key():
    try:
        os.unlink(_key_path)
    except OSError:
        pass

import pytest  # noqa: E402

from dba_slack_bot import config as cfg  # noqa: E402


@pytest.fixture(autouse=True)
def no_db_config(monkeypatch):
    """Make config reads return their declared default instead of hitting
    the DB. A missing default surfaces as KeyError (same as production),
    so a test that relies on an unset key fails loudly rather than
    silently connecting.

    BLIND SPOT, worth knowing before you trust a passing test: because this is
    autouse, `cfg.get_setting` NEVER RAISES under pytest. Production code that
    breaks when a config read fails — control DB down, pool not initialized —
    therefore looks fine here. A real bug hid in exactly that gap: web startup
    logged a banner containing `base_url()` (a config read) outside the try that
    was there to keep an unreachable DB from crash-looping the process, and no
    test could see it. A test for that class of failure must patch
    `cfg.get_setting` to raise, deliberately undoing this fixture — see
    tests/test_web_lifespan.py.
    """
    def fake_get_setting(key, default=None):
        if default is None:
            raise KeyError(f"bot_config key not set in test: {key}")
        return default
    monkeypatch.setattr(cfg, "get_setting", fake_get_setting)
    yield


class _RealDatabaseForbidden(BaseException):
    """Raised when a unit test reaches for a real database connection."""


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch, request):
    """Fail loudly instead of dialling out to a database.

    The suite is documented as pure-logic and mocked, but nothing enforced it.
    One test patched `db.transaction` and not `db.fetch_one`, so the code under
    test opened the connection pool for real and sat in `pool.wait()` until the
    30-second timeout — 30 of the suite's 35 seconds, in a test that passed and
    therefore never got questioned. A missed patch anywhere else would do the
    same thing, and on a machine with a reachable `localhost:5432` it would
    silently talk to whatever is listening there.

    So the pool is barred at the door: any unit test that reaches a real
    connection gets an immediate, named error instead of a slow pass. The
    real-DB integration tests opt out with `@pytest.mark.integration` (they
    already gate on QH_RUN_INTEGRATION).
    """
    if request.node.get_closest_marker("integration"):
        # yield, not return: this is a generator fixture, and returning early
        # makes pytest raise "did not yield a value" for every integration test.
        yield
        return

    def _refuse(*a, **k):
        # BaseException on purpose. Production code wraps DB reads in broad
        # `except Exception` blocks (correctly — a metadata read failing must
        # not lose a result), and an AssertionError would be swallowed by them:
        # the test would pass, quietly, having proved nothing. This has to be
        # unswallowable to mean anything.
        raise _RealDatabaseForbidden(
            "a unit test tried to open a real database connection. Patch the "
            "db call the code under test makes (db.transaction / db.fetch_one "
            "/ db.fetch_all / db.execute), or mark the test "
            "@pytest.mark.integration if it genuinely needs a database.")

    from dba_slack_bot import db
    monkeypatch.setattr(db, "init_pool", _refuse)
    monkeypatch.setattr(db, "_pool", None, raising=False)
    yield


@pytest.fixture(autouse=True)
def _logging_state_does_not_leak():
    """Restore global logging state after every test.

    Six test files call `logging.disable(logging.CRITICAL)` to keep expected
    error paths out of the pytest output, and only two of them turn it back on.
    `logging.disable` is process-global, so the rest leak: every test that runs
    afterwards has logging silenced.

    Silence is harmless for most tests and invisible — which is why it went
    unnoticed — but it makes any test that ASSERTS on log output pass or fail
    depending on file ordering. tests/test_logging_setup.py hit exactly that:
    green on its own, "nothing logged" in the full suite.

    Cheaper to reset here once than to fix six call sites and rely on the next
    one remembering.
    """
    yield
    logging.disable(logging.NOTSET)
