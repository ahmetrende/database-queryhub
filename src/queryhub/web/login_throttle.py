"""Brute-force throttle for the local (username/password) login.

Sliding-window failure counter per username AND per client IP: after
`web_local_login_max_failures` failed attempts (default 5) within
`web_local_login_window_minutes` (default 15), further attempts for that
key are rejected with 429 until the oldest failure slides out of the
window. A successful login clears the username's counter (not the IP's —
other accounts may still be under attack from the same address).

In-memory and per-process by design: the web app runs as a single
process, and a restart resetting the counters is acceptable (the window
is short). If the app is ever scaled to multiple processes, move this to
a small DB table — noted in docs/KNOWN_LIMITATIONS.md.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from .. import config as cfg

_LOCK = threading.Lock()
_FAILURES: dict[str, deque[float]] = {}
# Bound the map so a flood of distinct usernames/IPs can't grow it without
# limit (keys are otherwise pruned only when re-checked). When exceeded we
# sweep every key once, dropping those whose failures have all aged out.
_MAX_KEYS = 20_000


def _max_failures() -> int:
    return cfg.get_int("web_local_login_max_failures", 5)


def _window_seconds() -> int:
    return cfg.get_int("web_local_login_window_minutes", 15) * 60


def _prune(key: str, now: float, window: float) -> deque:
    q = _FAILURES.get(key)
    if q is None:
        q = _FAILURES[key] = deque()
    while q and now - q[0] > window:
        q.popleft()
    if not q:
        _FAILURES.pop(key, None)
    return q


def retry_after_seconds(*keys: str) -> int:
    """0 = allowed. Otherwise: seconds until the most-throttled key frees a
    slot (its oldest failure leaves the window)."""
    now = time.monotonic()
    window = _window_seconds()
    limit = _max_failures()
    worst = 0.0
    with _LOCK:
        for key in keys:
            q = _prune(key, now, window)
            if len(q) >= limit:
                worst = max(worst, window - (now - q[0]))
    return int(worst) + 1 if worst > 0 else 0


def _sweep(now: float, window: float) -> None:
    for k in list(_FAILURES.keys()):
        _prune(k, now, window)   # drops the key if all its failures aged out


def record_failure(*keys: str) -> None:
    now = time.monotonic()
    window = _window_seconds()
    with _LOCK:
        if len(_FAILURES) > _MAX_KEYS:
            _sweep(now, window)
        for key in keys:
            q = _prune(key, now, window)
            if not q:
                _FAILURES[key] = q = deque()
            q.append(now)


def clear(*keys: str) -> None:
    """On successful login: forget the given keys (username, not IP)."""
    with _LOCK:
        for key in keys:
            _FAILURES.pop(key, None)


def _reset_for_tests() -> None:
    with _LOCK:
        _FAILURES.clear()
