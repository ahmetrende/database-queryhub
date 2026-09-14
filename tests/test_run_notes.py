"""What ran, and what the server said while it ran.

A nine-statement role script finished as "Done — 0 row(s)" with an empty grid.
Every statement had run and the grants were on the target, but the only
evidence was in the audit log, so it was submitted twice (requests 5096 and
5097 on the same database, two minutes apart). The script's own output was
`RAISE NOTICE` lines, and those were dropped on the floor: the difference
between "Role created" and "Role already exists" never reached anyone.
"""
import json

from queryhub import executor as ex
from queryhub.web import mapping


class _Diag:
    """The shape psycopg hands a notice handler."""
    def __init__(self, text, severity="NOTICE"):
        self.message_primary = text
        self.severity_nonlocalized = severity
        self.severity = severity


def _res(index, leading, rows=0, notices=()):
    r = ex._StmtResult(index=index, leading=leading, rowcount=rows)
    r.notices = list(notices)
    return r


# --- collecting -------------------------------------------------------------

def test_a_notice_is_captured_with_its_severity():
    buf = []
    handle = ex._notice_collector(buf)
    handle(_Diag("Role created: app_user"))
    handle(_Diag("something is deprecated", "WARNING"))
    assert buf == [
        {"severity": "NOTICE", "text": "Role created: app_user"},
        {"severity": "WARNING", "text": "something is deprecated"},
    ]


def test_an_empty_notice_is_ignored():
    buf = []
    ex._notice_collector(buf)(_Diag("   "))
    assert buf == []


def test_the_collector_never_raises():
    # It runs on the driver's thread while a result streams. An exception
    # there would surface in unrelated code.
    buf = []
    ex._notice_collector(buf)(object())      # no attributes at all
    assert buf == []


def test_a_runaway_script_cannot_fill_the_row():
    buf = []
    handle = ex._notice_collector(buf)
    for i in range(ex._MAX_NOTICES + 50):
        handle(_Diag(f"row {i}"))
    assert len(buf) == ex._MAX_NOTICES


def test_a_long_notice_is_clipped():
    buf = []
    ex._notice_collector(buf)(_Diag("x" * 5000))
    assert len(buf[0]["text"]) == ex._MAX_NOTICE_CHARS


# --- the record -------------------------------------------------------------

def test_run_notes_summarise_the_statements_and_keep_the_notices():
    notes = ex._run_notes([
        _res(1, "DO", notices=[{"severity": "NOTICE", "text": "Role created"}]),
        _res(2, "GRANT"),
        _res(3, "GRANT"),
    ])
    assert notes["statements"] == [
        {"i": 1, "leading": "DO", "rows": 0, "snippet": ""},
        {"i": 2, "leading": "GRANT", "rows": 0, "snippet": ""},
        {"i": 3, "leading": "GRANT", "rows": 0, "snippet": ""},
    ]
    assert notes["notices"] == [
        {"i": 1, "severity": "NOTICE", "text": "Role created"}]
    assert notes["truncated"] is False


def test_notes_are_written_even_with_no_notices_at_all():
    # The operator's ask: say how many statements ran, notice or not.
    notes = ex._run_notes([_res(1, "SELECT", rows=12)])
    assert notes["statements"] == [
        {"i": 1, "leading": "SELECT", "rows": 12, "snippet": ""}]
    assert notes["notices"] == []


# --- rendering --------------------------------------------------------------

def _messages(notes):
    return mapping._run_note_messages({"run_notes": notes}, "2026-08-24 15:09:42.100")


def test_the_summary_counts_each_kind():
    notes = {"statements": [{"i": 1, "leading": "DO"}]
             + [{"i": i, "leading": "GRANT"} for i in range(2, 7)]
             + [{"i": i, "leading": "ALTER"} for i in range(7, 10)],
             "notices": []}
    text = _messages(notes)[0]["text"]
    assert text == "9 statements executed: 5 GRANT, 3 ALTER, DO."


def test_one_statement_reads_as_one():
    notes = {"statements": [{"i": 1, "leading": "SELECT"}], "notices": []}
    assert _messages(notes)[0]["text"] == "1 statement executed: SELECT."


def test_a_notice_names_the_statement_that_raised_it():
    notes = {"statements": [{"i": 1, "leading": "DO"}, {"i": 2, "leading": "GRANT"}],
             "notices": [{"i": 1, "severity": "NOTICE", "text": "Role created: app"}]}
    texts = [m["text"] for m in _messages(notes)]
    assert "NOTICE (statement 1): Role created: app" in texts


def test_a_single_statement_notice_needs_no_position():
    notes = {"statements": [{"i": 1, "leading": "DO"}],
             "notices": [{"i": 1, "severity": "NOTICE", "text": "done"}]}
    assert "NOTICE: done" in [m["text"] for m in _messages(notes)]


def test_a_warning_reads_as_an_error_line():
    notes = {"statements": [{"i": 1, "leading": "DO"}],
             "notices": [{"i": 1, "severity": "WARNING", "text": "no such role"}]}
    kinds = {m["text"]: m["kind"] for m in _messages(notes)}
    assert kinds["WARNING: no such role"] == "err"


def test_json_text_from_the_driver_is_parsed():
    notes = json.dumps({"statements": [{"i": 1, "leading": "SELECT"}], "notices": []})
    assert _messages(notes)[0]["text"] == "1 statement executed: SELECT."


def test_a_row_without_notes_adds_nothing():
    assert mapping._run_note_messages({}, "t") == []
    assert mapping._run_note_messages({"run_notes": "not json"}, "t") == []


def test_the_feed_carries_the_summary_before_the_row_count():
    row = {
        "query": "GRANT SELECT ON ALL TABLES IN SCHEMA public TO app;",
        "created_at": None, "status": "completed", "row_count": 0,
        "run_notes": {"statements": [{"i": 1, "leading": "GRANT"},
                                     {"i": 2, "leading": "GRANT"}],
                      "notices": []},
    }
    texts = [m["text"] for m in mapping.status_messages(row)]
    assert "2 statements executed: 2 GRANT." in texts
    assert texts.index("2 statements executed: 2 GRANT.") < texts.index("Done — 0 row(s).")
