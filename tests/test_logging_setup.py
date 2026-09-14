"""LOG_FORMAT=json — one JSON object per line, and never a raised exception.

Both entry points used to call basicConfig with a free-text format, so shipping
these logs anywhere queryable meant maintaining a regex — which breaks the first
time a message contains a colon, and every message here does (`"target 52:
timeout"`).

The properties that matter:

  * text remains the default and remains unchanged. A human tailing journalctl
    is the common case and JSON is worse for that.
  * every line parses as JSON, including the awkward ones: tracebacks
    (multi-line), non-ASCII, and values that are not JSON-serialisable.
  * a logging call NEVER raises. A log line that takes the process down converts
    an incident into an outage, so an unserialisable `extra=` value must
    degrade, not explode.
  * `extra=` fields survive into the output as fields, because that is the whole
    reason to emit JSON — a request id you have to regex back out of prose is
    not structured logging.
"""
import json
import logging

import pytest

from queryhub import logging_setup


@pytest.fixture
def capture(monkeypatch):
    """Configure logging into a list and hand back the emitted lines."""
    lines: list[str] = []

    class ListStream:
        def write(self, s):
            if s.strip():
                lines.append(s.rstrip("\n"))

        def flush(self):
            pass

    def configure(fmt):
        logging_setup.configure(level="DEBUG", fmt=fmt)
        # Re-point the handler basicConfig installed at our list.
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler):
                h.setStream(ListStream())
        return lines

    yield configure
    logging.disable(logging.NOTSET)
    logging_setup.configure(level="CRITICAL", fmt="text")


def test_text_is_the_default_and_is_not_json(capture, monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    lines = capture(None)
    logging.getLogger("t").info("hello")
    assert lines, "nothing logged"
    with pytest.raises(json.JSONDecodeError):
        json.loads(lines[-1])
    assert "hello" in lines[-1]


def test_json_lines_parse_and_carry_the_standard_fields(capture):
    lines = capture("json")
    logging.getLogger("queryhub.executor").warning("query timed out")
    obj = json.loads(lines[-1])
    assert obj["message"] == "query timed out"
    assert obj["level"] == "WARNING"
    assert obj["logger"] == "queryhub.executor"
    # RFC 3339 in UTC — correlating two hosts across a DST boundary is the kind
    # of problem that costs an hour at 3am.
    assert obj["timestamp"].endswith("+00:00")


def test_printf_style_arguments_are_interpolated(capture):
    """`log.info("target %s: %d rows", alias, n)` is the idiom used throughout
    the codebase. If the formatter emitted the template instead of the message,
    every log line would lose its data."""
    lines = capture("json")
    logging.getLogger("t").info("target %s: %d rows", "demo-primary", 42)
    assert json.loads(lines[-1])["message"] == "target demo-primary: 42 rows"


def test_a_traceback_stays_on_one_line(capture):
    """The reason JSON logging exists: a multi-line traceback in text format
    arrives at the log backend as N unrelated lines, and the one with the
    exception type is not the one with the context."""
    lines = capture("json")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("execution failed")
    assert len(lines) == 1, "the traceback was split across lines"
    obj = json.loads(lines[-1])
    assert obj["message"] == "execution failed"
    assert "ValueError: boom" in obj["exception"]
    assert "Traceback" in obj["exception"]


def test_extra_fields_become_queryable_fields(capture):
    lines = capture("json")
    logging.getLogger("t").info("dispatched",
                                extra={"request_id": 4242, "tier": "rw"})
    obj = json.loads(lines[-1])
    assert obj["request_id"] == 4242
    assert obj["tier"] == "rw"


def test_an_unserialisable_extra_degrades_instead_of_raising(capture):
    """A logging call must never take the process down. If it could, the first
    unexpected object someone passes in `extra=` turns an incident into an
    outage."""
    class Opaque:
        def __repr__(self):
            return "<Opaque>"

    lines = capture("json")
    logging.getLogger("t").info("weird", extra={"thing": Opaque()})
    obj = json.loads(lines[-1])
    assert obj["thing"] == "<Opaque>"


def test_non_ascii_is_kept_readable(capture):
    """Query text and database identifiers reach the logs. Escaping them to
    \\uXXXX makes the line correct but unreadable, which defeats the purpose."""
    lines = capture("json")
    # Any non-ASCII will do; the repo is English-only, so this is deliberately
    # not a phrase in any operator's language.
    logging.getLogger("t").info("column \u00e9l\u00e8ve on table \u4f8b")
    assert "\u00e9l\u00e8ve" in lines[-1]
    assert json.loads(lines[-1])["message"] == "column \u00e9l\u00e8ve on table \u4f8b"


def test_a_message_containing_json_does_not_corrupt_the_line(capture):
    lines = capture("json")
    logging.getLogger("t").info('payload was {"a": 1, "b": "}"}')
    obj = json.loads(lines[-1])
    assert obj["message"] == 'payload was {"a": 1, "b": "}"}'


def test_configure_replaces_handlers_rather_than_stacking_them(capture):
    """Called twice — or after a library configured logging on import — two
    handlers would duplicate every line, which is how log volume doubles for no
    reason."""
    capture("json")
    logging_setup.configure(level="DEBUG", fmt="json")
    logging_setup.configure(level="DEBUG", fmt="json")
    assert len(logging.getLogger().handlers) == 1


def test_noisy_slack_libraries_stay_at_warning(capture):
    capture("json")
    assert logging.getLogger("slack_bolt").level == logging.WARNING
    assert logging.getLogger("slack_sdk").level == logging.WARNING
