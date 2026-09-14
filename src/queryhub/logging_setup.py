"""Logging configuration, shared by both entry points.

There was no structured option: both processes called `basicConfig` with
`"%(asctime)s %(levelname)s %(name)s: %(message)s"`, so anyone shipping these
logs to Loki, CloudWatch Insights, or an ELK stack had to write and maintain a
regex — and a regex over free-text log lines breaks the first time a message
contains a colon.

`LOG_FORMAT=json` emits one JSON object per line instead. `text` (the default)
is unchanged, because a human tailing journalctl is the common case and JSON is
worse for that.

Read from the environment rather than `bot_config` on purpose. Logging is
configured once at process start, before the database pool exists — and a
config value that only takes effect on restart is more honest as an env var,
sitting next to `LOG_LEVEL` which has the same constraint.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os

# Attributes LogRecord always carries. Anything outside this set was attached by
# the caller via `extra=`, and belongs in the JSON output — that is how a
# request id or a target alias gets into a log pipeline as a queryable field
# instead of being interpolated into prose.
_STANDARD = frozenset((
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
))


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Field names follow the shape most log backends already understand
    (`timestamp`, `level`, `logger`, `message`), so the common queries work
    without a custom pipeline stage.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            # RFC 3339 in UTC. Not the local zone: correlating two hosts across
            # a DST boundary is the kind of problem that costs an hour at 3am.
            "timestamp": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            # Anything not JSON-serialisable becomes its repr rather than
            # taking the whole line down. A logging call must never raise.
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        # default=str so an unexpected type in `message` cannot fail the dump
        # either. ensure_ascii=False keeps non-ASCII readable rather than
        # escaped, which matters for any query text that reaches a log.
        return json.dumps(payload, default=str, ensure_ascii=False)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure(level: str | None = None, fmt: str | None = None) -> None:
    """Install the root handler. Idempotent-ish: `force=True` replaces any
    handler already installed, so a second call (or a library that configured
    logging on import) cannot leave two handlers duplicating every line.
    """
    level = level or os.environ.get("LOG_LEVEL", "INFO")
    fmt = (fmt or os.environ.get("LOG_FORMAT", "text")).strip().lower()

    handler = logging.StreamHandler()
    if fmt in ("json", "structured"):
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))

    logging.basicConfig(level=level, handlers=[handler], force=True)

    # These two are chatty at INFO and say nothing an operator wants.
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
