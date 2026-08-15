"""Graceful-drain lifecycle: the flag + the submit-path refusal.

On restart the process enters drain (bare bool); the submit path then refuses
new work so in-flight queries can finish. kill_switch defaults off in tests
(conftest returns config defaults), so validate_submission reaches the drain
check before any DB-touching step.
"""
from queryhub import core_submit, lifecycle


def test_lifecycle_flag(monkeypatch):
    monkeypatch.setattr(lifecycle, "_draining", False)   # revert point
    assert lifecycle.is_draining() is False
    lifecycle.begin_drain()
    assert lifecycle.is_draining() is True


def test_submit_refused_while_draining(monkeypatch):
    monkeypatch.setattr(lifecycle, "_draining", True)
    r = core_submit.validate_submission(
        "U1", "Tester", target_server_id=1, database_name="d",
        query="SELECT 1", justification=None)
    assert isinstance(r, core_submit.Rejection)
    assert r.field == "draining"
    assert "restart" in r.message.lower()
