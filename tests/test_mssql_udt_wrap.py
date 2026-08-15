"""Making SQL Server's UDT columns readable — and knowing when not to try.

`geography`, `geometry` and `hierarchyid` arrive as SQL Server's own
serialisation, which pyodbc hands over as opaque bytes. Only the server can turn
that into `POINT (28.9784 41.0082)` or `/1/2/3/`, so the executor asks it to, by
re-projecting those columns through `.ToString()`.

Measured against the live SQL Server 2025 before this shipped: the raw values are
hex, `sp_describe_first_result_set` names the types without executing, the
re-projection returns the readable strings, and a top-level `ORDER BY` makes the
query unwrappable — the server rejects a derived table with one. That last fact
is why `wrap_udt_projection` is allowed to be wrong: the executor tries it and
falls back. What must never be wrong is the set of cases it declines outright,
because those would change what the query returns.
"""
from dba_slack_bot import mssql_exec as m


def test_wraps_only_the_udt_columns():
    sql = m.wrap_udt_projection("SELECT 1", [("g", "geography"), ("n", "int")])
    assert sql is not None
    assert "qh.[g].ToString() AS [g]" in sql
    assert "qh.[n]" in sql and "qh.[n].ToString()" not in sql
    assert sql.endswith(") AS qh")


def test_all_three_udt_types_are_recognised():
    for ty in ("geography", "geometry", "hierarchyid"):
        assert m.wrap_udt_projection("SELECT 1", [("c", ty)]) is not None
    assert m.UDT_TYPE_NAMES == frozenset({"geography", "geometry", "hierarchyid"})


def test_nothing_to_gain_is_declined():
    assert m.wrap_udt_projection("SELECT 1", [("a", "int")]) is None
    assert m.wrap_udt_projection("SELECT 1", []) is None
    assert m.wrap_udt_projection("SELECT 1", None) is None


def test_an_unnamed_column_is_declined():
    """`SELECT geography::Point(…)` with no alias: the wrapper has no name to
    select it by, and inventing one renames a column the caller asked for."""
    assert m.wrap_udt_projection("SELECT 1", [("", "geography"), ("n", "int")]) is None


def test_a_duplicated_name_is_declined():
    """`SELECT a.id, b.id` — `qh.[id]` is ambiguous, and picking one silently
    drops the other. Declining leaves hex, which is honest."""
    assert m.wrap_udt_projection(
        "SELECT 1", [("id", "geography"), ("id", "int")]) is None


def test_a_bracket_in_a_column_name_is_escaped():
    """T-SQL escapes `]` by doubling it. Without this the generated statement is
    malformed, and the fallback would hide it as "the server refused"."""
    sql = m.wrap_udt_projection("SELECT 1", [("we]ird", "hierarchyid")])
    assert "[we]]ird]" in sql


def test_describe_columns_returns_none_when_the_server_declines():
    """Not every statement can be described. That is not an error — it means
    "do not wrap"."""
    class Cur:
        description = None
        def execute(self, *a, **k):
            raise RuntimeError("cannot be described")
    assert m.describe_columns(Cur(), "SELECT 1") is None


def test_describe_columns_lowercases_the_type_name():
    """The wrap decision compares against lowercase names; a server that returns
    `Geography` must not silently stop matching."""
    class Cur:
        description = [("name",), ("system_type_name",)]
        def execute(self, *a, **k): pass
        def fetchall(self): return [("g", "GEOGRAPHY"), ("n", "int")]
    assert m.describe_columns(Cur(), "SELECT 1") == [("g", "geography"), ("n", "int")]
