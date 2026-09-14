"""Unit tests for pii._column_skips — the pure column-exemption predicate,
incl. the opt-in apply_in_joins relaxation (migration 053)."""
from dba_slack_bot.pii import _column_skips


def _r(table, col, joins=False):
    return {"table_name": table, "column_name": col, "apply_in_joins": joins}


def test_column_scoped_matches_any_table():
    assert _column_skips([_r(None, "email")], {"a", "b"}, ["email"]) == {0}


def test_table_scoped_strict_single_table_only():
    rows = [_r("sessions", "title")]
    assert _column_skips(rows, {"sessions"}, ["title"]) == {0}
    # join -> provenance unknown -> NOT exempt (fail-closed)
    assert _column_skips(rows, {"sessions", "parent_events"}, ["title"]) == set()


def test_apply_in_joins_fires_when_table_present():
    rows = [_r("sessions", "title", joins=True)]
    assert _column_skips(rows, {"sessions", "parent_events"}, ["title"]) == {0}
    assert _column_skips(rows, {"sessions"}, ["title"]) == {0}      # single still works
    assert _column_skips(rows, {"users"}, ["title"]) == set()       # table absent -> no


def test_unparseable_sql_fail_closed_for_table_scoped():
    assert _column_skips([_r("sessions", "title", joins=True)], None, ["title"]) == set()
    # column-scoped still applies with unknown tables
    assert _column_skips([_r(None, "title")], None, ["title"]) == {0}


def test_column_name_must_match():
    assert _column_skips([_r("sessions", "title", joins=True)], {"sessions"}, ["id", "x"]) == set()
