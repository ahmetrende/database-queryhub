"""The daily cleanup trims the notification outbox.

The table had no removal at all: a row was written for every pending
submission and nothing ever deleted one. A pending-approval notice nobody
delivered within the window is stale, so rows go by age, processed or not.
"""
import importlib.util
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_old_results.py"


def _load():
    spec = importlib.util.spec_from_file_location("cleanup", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rows_older_than_the_window_are_deleted_processed_or_not(monkeypatch):
    mod = _load()
    calls = []
    monkeypatch.setattr(mod.db, "execute", lambda sql, params=None: calls.append((sql, params)))
    mod.cleanup_notification_outbox(7)
    (sql, params), = calls
    assert "DELETE FROM notification_outbox" in sql
    assert "created_at < %s" in sql
    assert "processed_at" not in sql
    age = datetime.now(timezone.utc) - params[0]
    assert timedelta(days=7) - timedelta(minutes=1) < age < timedelta(days=7, minutes=1)


def test_the_nightly_run_uses_the_retention_setting():
    src = inspect.getsource(_load().main)
    assert 'cleanup_notification_outbox(cfg.get_int("idp_outbox_retention_days", 7))' in src
