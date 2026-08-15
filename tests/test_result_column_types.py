"""Per-column result types captured from the driver's cursor description.

The web grid's header tooltip used to guess. It built a column-NAME -> type map
from the hourly schema snapshot and dropped any name that appeared in two tables
with a different definition — so `id`, `user_id` and `created_at`, the columns
people hover most, never had a type and the tooltip fell back to the bare name.
The guard made the feature useless for the case it was built for.

The driver knows. `executor._column_types` reads it while the cursor is still
open, which is the only moment it exists: the result is written to a CSV and the
web reads that file back in another process.

What these tests pin:
  * psycopg's `type_display` is preferred, because it carries modifiers and
    array-ness (`varchar(20)`, `numeric(10,2)`, `int4[]`) that a bare type name
    loses.
  * pyodbc's shape — a 7-tuple whose [1] is a Python type object — still yields
    something, so the SQL Server path is not silently typeless.
  * it NEVER raises. A tooltip must not be the reason a delivered result fails,
    so a driver with an unexpected description degrades to None.
"""
import pytest

from dba_slack_bot import executor


class PsycopgColumn:
    """Shape of psycopg3's Column: indexable AND attribute-bearing."""

    def __init__(self, name, type_display, oid=0):
        self.name = name
        self.type_display = type_display
        self.type_code = oid
        self.null_ok = None          # psycopg reports this for every column

    def __getitem__(self, i):
        return (self.name, self.type_code, None, None, None, None, self.null_ok)[i]

    def __len__(self):
        return 7


def types_of(desc, cur=None):
    """The two halves in the order the executor runs them.

    `_column_types` reads the driver's description and issues NO query;
    `_apply_resolved_types` does the catalog lookup, and only after the caller
    has closed the result portal. Tests go through this helper so they assert
    the end result while the split stays enforced by the tests below.
    """
    types, unknown = executor._column_types(desc)
    return executor._apply_resolved_types(types, unknown, cur)


def test_psycopg_type_display_is_used_verbatim():
    """The modifiers are the point. A name map could never carry them."""
    desc = [
        PsycopgColumn("id", "int4"),
        PsycopgColumn("code", "varchar(20)"),
        PsycopgColumn("amount", "numeric(10,2)"),
        PsycopgColumn("tags", "int4[]"),
        PsycopgColumn("meta", "jsonb"),
        PsycopgColumn("created_at", "timestamptz"),
    ]
    assert types_of(desc) == {
        "id": "int4", "code": "varchar(20)", "amount": "numeric(10,2)",
        "tags": "int4[]", "meta": "jsonb", "created_at": "timestamptz",
    }


def test_expressions_and_aggregates_get_a_type():
    """The catalog cannot answer these at all — there is no column to look up.
    `count(*)` is int8 and `upper(x)` is text, and the driver says so."""
    desc = [PsycopgColumn("n", "int8"), PsycopgColumn("upper", "text")]
    assert types_of(desc) == {"n": "int8", "upper": "text"}


def test_a_duplicate_column_name_keeps_the_first():
    """`SELECT a.id, b.id FROM ...` — the CSV header dedups names separately, so
    a map keyed on the raw name can only hold one. Keep the first rather than let
    the second silently overwrite it."""
    desc = [PsycopgColumn("id", "int4"), PsycopgColumn("id", "text")]
    assert types_of(desc) == {"id": "int4"}


def test_pyodbc_seven_tuple_still_yields_types():
    """pyodbc has no type_display; description[i][1] is a Python type object.
    Coarser than a SQL type name, but the SQL Server path must not come back
    empty — an empty tooltip there would look like the old bug."""
    desc = [
        ("id", int, None, 10, 10, 0, True),
        ("name", str, None, 50, 50, 0, True),
        ("ts", __import__("datetime").datetime, None, 23, 23, 3, True),
    ]
    out = types_of(desc)
    assert out == {"id": "int", "name": "str(50)", "ts": "datetime"}


def test_pyodbc_size_is_only_added_for_strings():
    """A size on an int is the display width, which is noise. On a varchar it is
    the declared length, which is the useful part."""
    desc = [("n", int, None, 10, 10, 0, True), ("s", str, None, 8, 8, 0, True)]
    out = types_of(desc)
    assert out["n"] == "int"        # no (10)
    assert out["s"] == "str(8)"


@pytest.mark.parametrize("desc", [
    None,                                   # driver gave nothing
    [],                                     # no columns
    [("", str, None, None, None, None, None)],   # unnamed column
    [object()],                             # not indexable at all
])
def test_never_raises_and_returns_none_when_there_is_nothing_to_say(desc):
    """A result is already delivered by the time this runs. Failing here would
    turn a working query into a failed one over a tooltip."""
    assert types_of(desc) is None


def test_a_column_whose_type_cannot_be_determined_is_simply_absent():
    """Partial data beats no data: the columns that DO have a type still get
    one, and the frontend falls back for the rest."""
    desc = [PsycopgColumn("good", "int4"), ("bad", None, None, None, None, None, None)]
    assert types_of(desc) == {"good": "int4"}


def test_the_captured_types_are_json_serialisable():
    """They go into a JSONB column, so a value that json.dumps cannot handle
    would fail the completion UPDATE — after the query already ran."""
    import json
    desc = [PsycopgColumn("id", "int4"), ("s", str, None, 8, 8, 0, True)]
    assert json.loads(json.dumps(types_of(desc))) == {
        "id": "int4", "s": "str(8)"}


# ---------------------------------------------------------------------------
# User-defined types. Found live, not imagined: the control DB's own
# `requests.status` is an enum, and psycopg has no adapter for it, so
# type_display returned the string "42887" — the raw OID. A number in a tooltip
# is worse than a bare column name, so an unnamed type is resolved through
# pg_type and dropped if even that fails.
# ---------------------------------------------------------------------------

class FakeCursor:
    """Answers the pg_type lookup. `rows` decides the row factory being
    simulated — dicts for the control-plane pool, tuples for a target."""

    def __init__(self, rows, fail=False):
        self.rows = rows
        self.fail = fail
        self.executed = []

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("permission denied for table pg_type")
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


def test_an_enum_oid_is_resolved_through_pg_type():
    desc = [PsycopgColumn("id", "int8"), PsycopgColumn("status", "42887")]
    cur = FakeCursor([{"oid": 42887, "fmt": "request_status"}])
    assert types_of(desc, cur) == {
        "id": "int8", "status": "request_status"}
    # Only the UNKNOWN oid is looked up — a result of all builtin types must
    # cost no extra query.
    assert len(cur.executed) == 1
    assert cur.executed[0][1] == ([42887],)


def test_no_lookup_happens_when_every_type_is_known():
    desc = [PsycopgColumn("id", "int8"), PsycopgColumn("name", "text")]
    cur = FakeCursor([])
    types_of(desc, cur)
    assert cur.executed == [], "spent a query resolving nothing"


def test_tuple_rows_resolve_too():
    """The target connection yields tuples; the control-plane pool is configured
    with dict_row. Indexing by position worked in production and raised KeyError
    under a dict cursor — and it failed SILENTLY, dropping the type, which looks
    exactly like the bug being fixed. Both shapes must work."""
    desc = [PsycopgColumn("status", "42887")]
    assert types_of(desc, FakeCursor([(42887, "request_status")])) \
        == {"status": "request_status"}
    assert types_of(desc, FakeCursor([{"oid": 42887, "fmt": "request_status"}])) \
        == {"status": "request_status"}


def test_an_unresolvable_oid_is_dropped_not_shown_as_a_number():
    """Falling back to the schema catalog beats rendering '42887'."""
    desc = [PsycopgColumn("id", "int8"), PsycopgColumn("status", "42887")]
    assert types_of(desc, FakeCursor([])) == {"id": "int8"}


def test_a_failed_lookup_drops_the_column_and_keeps_the_rest():
    desc = [PsycopgColumn("id", "int8"), PsycopgColumn("status", "42887")]
    assert types_of(desc, FakeCursor([], fail=True)) == {"id": "int8"}


def test_without_a_cursor_an_unknown_type_is_still_not_a_number():
    """The MSSQL path passes no cursor. It must not start emitting OIDs."""
    desc = [PsycopgColumn("id", "int8"), PsycopgColumn("status", "42887")]
    assert types_of(desc) == {"id": "int8"}


# ---------------------------------------------------------------------------
# The production wedge, 2026-07-29. These are the tests the 1139 that already
# existed could not be: every one of them passed while the bot was hung.
#
# `res.col_types = _column_types(cur.description, cur)` ran BEFORE the result
# was streamed. A stream stopped at the row cap leaves the portal open — the
# server is still holding the rest of the result — so the catalog lookup queued
# behind it and never returned. psycopg has no client-side timeout there, so the
# single `sql-exec` worker blocked forever. Every later request stayed in
# `approved`, which the UI renders as running, and the runaway watchdog quietly
# force-failed the row so the database looked clean. It ended with a restart.
#
# It needed BOTH a truncated result AND a user-defined type in it, which is why
# it looked intermittent and why no unit test came close.
# ---------------------------------------------------------------------------


def test_reading_the_types_costs_no_query_at_all():
    """The invariant that makes the wedge impossible: the half that runs while
    the portal is open cannot talk to the server, because it has nothing to talk
    to it with."""
    desc = [PsycopgColumn("id", "int8"), PsycopgColumn("status", "42887")]
    types, unknown = executor._column_types(desc)
    assert types == {"id": "int8", "status": "42887"}
    assert unknown == {42887}, "the OID must be reported, not resolved"


def test_the_reading_half_cannot_be_handed_a_cursor_again():
    """Guard against the coupling coming back. Re-adding the parameter is how
    the bug returns, and it would look like a harmless tidy-up."""
    desc = [PsycopgColumn("status", "42887")]
    with pytest.raises(TypeError):
        executor._column_types(desc, FakeCursor([]))          # type: ignore[call-arg]


def test_the_executor_closes_the_stream_before_the_lookup():
    """An ordering invariant, so it is asserted on the source: there is no way
    to observe it from outside, and getting it wrong costs an outage rather than
    a wrong value.

    A real-server proof of the same thing is the integration test below."""
    import inspect
    src = inspect.getsource(executor._execute_main_statement)
    close_at = src.find("stream.close()")
    apply_at = src.find("_apply_resolved_types(res.col_types, _unknown_oids, cur)")
    assert close_at != -1, "the result stream is never closed explicitly"
    assert apply_at != -1, "the type lookup no longer runs after streaming"
    assert close_at < apply_at, (
        "the catalog lookup runs before the portal is closed — this is the "
        "exact ordering that wedged the executor")
    # And the reading half must not be given the cursor at the top any more.
    assert "_column_types(cur.description)" in src


def test_the_early_returns_do_not_run_the_lookup_either():
    """EXPLAIN and wants_result=False both leave the portal open. They must drop
    unresolved labels rather than reach for the catalog."""
    import inspect
    src = inspect.getsource(executor._execute_main_statement)
    assert src.count("_apply_resolved_types(res.col_types, _unknown_oids, None)") == 2
