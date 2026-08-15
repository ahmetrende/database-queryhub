"""Sliding-window brute-force throttle for local login (7.1). Pure — time is
monkeypatched, config comes from the conftest defaults (5 fails / 15 min)."""
import pytest

from queryhub.web import login_throttle as lt


@pytest.fixture(autouse=True)
def clean_state():
    lt._reset_for_tests()
    yield
    lt._reset_for_tests()


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1000.0}
    monkeypatch.setattr(lt.time, "monotonic", lambda: state["now"])
    return state


def test_allows_under_limit(clock):
    for _ in range(4):
        lt.record_failure("u:alice", "ip:1.2.3.4")
    assert lt.retry_after_seconds("u:alice", "ip:1.2.3.4") == 0


def test_locks_at_limit(clock):
    for _ in range(5):
        lt.record_failure("u:alice", "ip:1.2.3.4")
    wait = lt.retry_after_seconds("u:alice", "ip:1.2.3.4")
    assert 0 < wait <= 15 * 60 + 1


def test_window_slides_open(clock):
    for _ in range(5):
        lt.record_failure("u:alice")
    assert lt.retry_after_seconds("u:alice") > 0
    clock["now"] += 15 * 60 + 1          # oldest failure leaves the window
    assert lt.retry_after_seconds("u:alice") == 0


def test_success_clears_username_but_not_ip(clock):
    for _ in range(5):
        lt.record_failure("u:alice", "ip:1.2.3.4")
    lt.clear("u:alice")                   # successful login for alice
    assert lt.retry_after_seconds("u:alice") == 0
    # the shared IP stays throttled — other accounts may be under attack
    assert lt.retry_after_seconds("ip:1.2.3.4") > 0


def test_keys_are_independent(clock):
    for _ in range(5):
        lt.record_failure("u:alice")
    assert lt.retry_after_seconds("u:bob") == 0
    assert lt.retry_after_seconds("u:alice") > 0


def test_sweep_drops_aged_keys_over_cap(clock, monkeypatch):
    monkeypatch.setattr(lt, "_MAX_KEYS", 3)
    for i in range(5):                       # 5 distinct aged-out keys
        lt.record_failure(f"ip:old{i}")
    clock["now"] += 15 * 60 + 1              # everything ages out
    lt.record_failure("ip:fresh")            # >cap -> triggers a sweep
    # the aged keys are gone; only the fresh one remains
    assert list(lt._FAILURES.keys()) == ["ip:fresh"]


def test_lock_expires_gradually(clock):
    # 5 failures spread one minute apart: the lock lifts when the OLDEST
    # slides out, i.e. window - age(oldest).
    for i in range(5):
        clock["now"] = 1000.0 + i * 60
        lt.record_failure("u:alice")
    clock["now"] = 1000.0 + 5 * 60
    wait = lt.retry_after_seconds("u:alice")
    assert 0 < wait <= 10 * 60 + 1        # oldest is 5 min old of a 15-min window