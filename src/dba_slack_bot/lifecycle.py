"""Process lifecycle — graceful drain on restart.

When the process gets a stop signal (systemd restart), it enters *drain*: new
submissions are refused (`is_draining()` → the submit path returns a friendly
Rejection) so nothing new starts, while in-flight executions are left to finish
— the executor's thread pool is shut down with `wait=True`, bounded by systemd
`TimeoutStopSec`. Process-local: each service drains only itself.

This adds NO latency to an idle restart: the flag is a bare bool, and the pool
shutdown returns immediately when nothing is running. The bound only applies
when a real query is actually mid-flight.
"""
from __future__ import annotations

_draining = False


def begin_drain() -> None:
    """Enter drain — called from the stop-signal handler. Idempotent."""
    global _draining
    _draining = True


def is_draining() -> bool:
    return _draining
