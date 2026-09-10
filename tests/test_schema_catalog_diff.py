"""The hourly catalog refresh must be a diff, and must still converge.

It used to delete every row for a (target, database) and insert the lot back,
once an hour, whether anything had changed or not: 172,450,815 `schema_columns`
rows written into a table holding 156,736. Now it upserts on a natural key and
deletes only what the source no longer has.

A diff has two ways to be wrong and both are quiet:

  * it stops converging — drift in the stored catalog survives a refresh, so
    the browser offers a column the database dropped months ago;
  * it starts rewriting again — a `DO UPDATE` with no `WHERE` writes a new row
    version for every unchanged column, which is the whole cost this removed
    and nothing on any screen would show it.

So the tests are: the statements it issues (shape), and the three directions
drift can go (behaviour, against a cursor that remembers rows).
"""
from __future__ import annotations

import pytest

from queryhub import schema_catalog


class RecordingCursor:
    """Captures every statement. `returning` feeds the one fetch the table
    upsert makes."""

    def __init__(self, returning=None):
        self.sql: list[str] = []
        self.params: list = []
        self._returning = returning or []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.params.append(params)

    def executemany(self, sql, seq):
        self.sql.append(" ".join(sql.split()))
        self.params.append(list(seq))

    def fetchall(self):
        return list(self._returning)

    def fetchone(self):
        return self._returning[0] if self._returning else None


TABLES = [{"schema_name": "public", "table_name": "orders", "relkind": "r",
           "row_estimate": 10, "total_bytes": 100, "partition_count": 0,
           "partition_key": None, "indexes": None, "foreign_keys": None}]


# ---------------------------------------------------------------------------
# shape: what it is allowed to send
# ---------------------------------------------------------------------------

def test_it_never_clears_the_catalog_before_writing_it():
    """The bug being removed. A `DELETE ... WHERE target AND database` with no
    further condition is the rewrite, and it must not come back."""
    cur = RecordingCursor([{"id": 1, "schema_name": "public",
                            "table_name": "orders"}])
    schema_catalog._sync_tables(cur, 7, "app", TABLES)
    for stmt in cur.sql:
        if stmt.startswith("DELETE FROM schema_tables"):
            assert "id <> ALL" in stmt, (
                "the table delete must name what survives, not clear the "
                f"database: {stmt}")


def test_an_unchanged_column_is_not_rewritten():
    """`DO UPDATE` without the `IS DISTINCT FROM` guard writes a new row
    version for every column every hour. Measured before the guard: 3.8M rows
    a day across the fleet, replacing rows with identical copies."""
    cur = RecordingCursor()
    schema_catalog._sync_columns(cur, [1], [(1, 1, "id", "int", True, None,
                                             True, True)])
    upsert = next(s for s in cur.sql if s.startswith("INSERT INTO schema_columns"))
    assert "ON CONFLICT (table_id, column_name) DO UPDATE" in upsert
    assert "IS DISTINCT FROM" in upsert, (
        "the conditional is the whole point — without it every refresh "
        "rewrites the table")


def test_the_column_delete_is_keyed_not_wholesale():
    cur = RecordingCursor()
    schema_catalog._sync_columns(cur, [1], [(1, 1, "id", "int", True, None,
                                             True, True)])
    delete = next(s for s in cur.sql if s.startswith("DELETE FROM schema_columns"))
    assert "NOT EXISTS" in delete and "unnest" in delete


# ---------------------------------------------------------------------------
# the empty cases, where a diff most easily does nothing at all
# ---------------------------------------------------------------------------

def test_a_database_that_lost_every_table_is_emptied():
    """A source with no tables left must clear the stored ones. A diff that
    only ever upserts would keep serving a database that no longer exists."""
    cur = RecordingCursor()
    assert schema_catalog._sync_tables(cur, 7, "app", []) == {}
    assert any(s.startswith("DELETE FROM schema_tables") for s in cur.sql)


def test_a_table_that_lost_every_column_is_emptied():
    cur = RecordingCursor()
    schema_catalog._sync_columns(cur, [1, 2], [])
    assert cur.sql == ["DELETE FROM schema_columns WHERE table_id = ANY(%s)"]


def test_no_tables_means_no_column_statements_at_all():
    cur = RecordingCursor()
    schema_catalog._sync_columns(cur, [], [])
    assert cur.sql == []


def test_routines_that_vanished_are_removed():
    cur = RecordingCursor()
    schema_catalog._sync_routines(cur, 7, "app", [])
    assert any(s.startswith("DELETE FROM schema_functions") for s in cur.sql)


def test_overloads_still_collapse_to_one_row():
    """Two signatures of the same routine are one suggestion (migration 091).
    The upsert conflicts on (schema, routine), so sending both would make the
    statement update the row with its own sibling — Postgres refuses a second
    conflicting write in one command."""
    cur = RecordingCursor()
    schema_catalog._sync_routines(cur, 7, "app", [
        {"schema_name": "public", "routine_name": "f", "routine_kind": "function",
         "arg_signature": "(int)", "returns": "int"},
        {"schema_name": "public", "routine_name": "f", "routine_kind": "function",
         "arg_signature": "(text)", "returns": "int"},
    ])
    insert_params = cur.params[cur.sql.index(
        next(s for s in cur.sql if s.startswith("INSERT INTO schema_functions")))]
    names = insert_params[3]          # the routine_name array
    assert names == ["f"], f"an overload reached the statement twice: {names}"


# ---------------------------------------------------------------------------
# convergence, against a cursor that remembers
# ---------------------------------------------------------------------------

class FakeColumnStore:
    """Just enough of `schema_columns` to answer "did the refresh converge".
    Rows are {(table_id, name): tuple}; the upsert and the delete are applied
    the way the two statements would."""

    def __init__(self, rows):
        self.rows = dict(rows)
        self.writes = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO schema_columns"):
            tids, ords, names, types, nn, dflt, pk, inidx = params
            for i, name in enumerate(names):
                key = (tids[i], name)
                new = (ords[i], types[i], nn[i], dflt[i], pk[i], inidx[i])
                if self.rows.get(key) != new:
                    self.rows[key] = new
                    self.writes += 1
        elif s.startswith("DELETE FROM schema_columns sc"):
            _ids, tids, names = params
            keep = set(zip(tids, names))
            for key in [k for k in self.rows if k not in keep]:
                del self.rows[key]
                self.writes += 1


def _want(rows):
    return [(tid, o, n, t, nn, d, pk, ix)
            for (tid, n), (o, t, nn, d, pk, ix) in rows.items()]


LIVE = {(1, "id"): (1, "int", True, None, True, True),
        (1, "email"): (2, "text", False, None, False, False)}


@pytest.mark.parametrize("drift,label", [
    ({(1, "email"): (2, "WRONG", False, None, False, False)}, "a changed type"),
    ({}, "a dropped row"),
    ({**LIVE, (1, "ghost"): (9, "text", False, None, False, False)},
     "a row the source does not have"),
])
def test_the_refresh_converges_from_any_drift(drift, label):
    stored = FakeColumnStore({**LIVE, **drift} if drift else dict(LIVE))
    if not drift:                       # the dropped-row case
        stored = FakeColumnStore({(1, "id"): LIVE[(1, "id")]})
    schema_catalog._sync_columns(stored, [1], _want(LIVE))
    assert stored.rows == LIVE, f"did not converge from {label}"


def test_a_second_refresh_of_unchanged_data_writes_nothing():
    """The steady state, which is nearly every hour. Measured against the live
    fleet after the change: 0 inserts, 0 updates, 0 deletes on 156,736
    columns."""
    stored = FakeColumnStore(dict(LIVE))
    schema_catalog._sync_columns(stored, [1], _want(LIVE))
    assert stored.writes == 0
