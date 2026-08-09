"""Disabling a target must stop queries, not just submissions.

`enabled` gated the pickers, the connection list and the submit path, so nothing
NEW could be sent to a disabled target — and a request that was already approved,
queued or scheduled ran against it anyway, because `targets.get()` returns the row
regardless of the flag and the executor never looked.

That is the wrong meaning for the flag. An operator disables a target when the
host is being migrated, decommissioned, or is in an incident; "no more
submissions" is not what they are asking for.

The check lives in the executor rather than in `targets.get()` on purpose: the
browse, admin and audit paths legitimately need to SEE a disabled target, and
hiding it from them would be a different bug.
"""
import pytest

from queryhub import executor as ex


def _target(enabled: bool):
    return type("T", (), {"enabled": enabled, "engine": "postgres",
                          "alias": "svc-prod-orders", "id": 7,
                          "default_database": "app", "host": "h",
                          "port": 5432})()


@pytest.fixture()
def failures(monkeypatch):
    """Capture _fail() instead of touching the DB or Slack."""
    seen = []
    monkeypatch.setattr(ex, "_fail", lambda client, req, msg: seen.append(msg))
    return seen


def _request():
    return {"id": 4321, "target_server_id": 7, "database_name": "app",
            "query": "SELECT 1", "requester_slack_id": "U1", "status": "approved"}


def test_a_disabled_target_refuses_before_anything_connects(monkeypatch, failures):
    monkeypatch.setattr(ex.targets, "get", lambda tid: _target(False))
    # If the guard did not fire, execution would continue to credentials. Make
    # that loud rather than letting a later stub absorb it.
    monkeypatch.setattr(ex.targets, "get_credentials",
                        lambda *a, **k: pytest.fail(
                            "reached credentials for a DISABLED target"))

    ex._run(_request(), client=None)

    assert failures, "a disabled target produced no failure at all"
    assert "disabled" in failures[0].lower()
    assert "svc-prod-orders" in failures[0], (
        "the message must name the target, or the requester cannot ask anyone "
        "why it is off")


class _Stop(BaseException):
    """BaseException so `_run`'s `except Exception` cannot swallow it — the point
    is to observe how far execution got, not to let the function recover."""


def test_an_enabled_target_is_not_stopped_by_this_guard(monkeypatch, failures):
    """The other half. The guard must not become the reason ordinary requests
    fail, so pin that an ENABLED target reaches the step immediately after it —
    the engine-executability check — rather than asserting on the absence of a
    message, which would also pass if `_run` died even earlier."""
    monkeypatch.setattr(ex.targets, "get", lambda tid: _target(True))
    reached = []

    def _next_step(engine):
        reached.append(engine)
        raise _Stop

    monkeypatch.setattr(ex.engines, "is_executable", _next_step)

    with pytest.raises(_Stop):
        ex._run(_request(), client=None)

    assert reached == ["postgres"], "the guard stopped an enabled target"
    assert not any("disabled" in m.lower() for m in failures), failures


def test_the_error_handler_cannot_itself_crash_before_the_run_starts():
    """`committed` is read by the except handler to decide whether a mutation had
    already landed. It used to be assigned INSIDE the try, several checks in, so
    anything raising earlier — including the new guard above — hit an
    UnboundLocalError in the error handler, which is the one place that must not
    fail. It is now assigned before the try; this pins that.
    """
    import inspect
    src = inspect.getsource(ex._run)
    body = src.split("try:", 1)
    assert 'committed = {"mutation": False}' in body[0], (
        "committed is assigned inside the try again — an early failure will "
        "crash the error handler instead of reporting the error")
