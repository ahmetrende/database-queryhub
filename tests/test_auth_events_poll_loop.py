"""The auth-event DM poller must outlive a failed config read.

On 2026-10-10 the metadata database blipped. The poll interval was read in the
`while` condition, outside the `try`, so the read raised and the thread ended.
No DM went out for two and a half days, until a restart. The interval is now read
by `_poll_seconds`, which keeps the last good value when the read fails.
"""
from __future__ import annotations

import pytest

from queryhub import auth_events


class _Stop:
    """threading.Event stand-in. wait() returns False `ticks` times, then True."""

    def __init__(self, ticks: int):
        self.left = ticks
        self.timeouts: list = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        if self.left > 0:
            self.left -= 1
            return False
        return True


@pytest.fixture
def loop(monkeypatch):
    """Run poll_loop for N ticks with the config read and the work faked."""
    state = {"interval": lambda: 20, "work": lambda: 0, "calls": 0}

    def get_int(key, default):
        assert key == "auth_event_poll_seconds"
        return state["interval"]()

    def process_pending(client):
        state["calls"] += 1
        return state["work"]()

    monkeypatch.setattr(auth_events.cfg, "get_int", get_int)
    monkeypatch.setattr(auth_events, "is_enabled", lambda: True)
    monkeypatch.setattr(auth_events, "process_pending", process_pending)

    def run(ticks):
        stop = _Stop(ticks)
        auth_events.poll_loop(object(), stop)
        return stop

    run.state = state
    return run


def test_a_failed_interval_read_does_not_end_the_loop(loop):
    def down():
        raise RuntimeError("the metadata database is down")

    loop.state["interval"] = down
    stop = loop(3)
    assert loop.state["calls"] == 3                     # every tick still did its work
    assert stop.timeouts == [20, 20, 20, 20]            # and kept the last good interval


def test_the_loop_keeps_the_last_good_interval_after_a_failure(loop):
    answers = iter([5, "fail", 7])

    def flaky():
        value = next(answers)
        if value == "fail":
            raise RuntimeError("blip")
        return value

    loop.state["interval"] = flaky
    stop = loop(3)
    assert stop.timeouts == [20, 5, 5, 7]


def test_the_interval_follows_the_setting(loop):
    loop.state["interval"] = lambda: 5
    assert loop(2).timeouts == [20, 5, 5]


@pytest.mark.parametrize("value", [0, -3])
def test_a_zero_or_negative_interval_is_floored_at_one_second(loop, value):
    loop.state["interval"] = lambda: value
    assert loop(2).timeouts == [20, 1, 1]


def test_a_failing_tick_does_not_end_the_loop_either(loop):
    def boom():
        raise RuntimeError("send failed")

    loop.state["work"] = boom
    loop(2)
    assert loop.state["calls"] == 2
