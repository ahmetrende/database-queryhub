"""Live table facts: numbers only, never a value of a column (M13).

`pg_stats` and the SQL Server statistics also hold real column values
(`most_common_vals`, `histogram_bounds`, histogram steps). The operator
allowed the numeric statistics only. These tests check that no query of the
two readers names a value-holding statistic, and that the rows map to the
answer.
"""
from __future__ import annotations

from datetime import datetime, timezone

from queryhub import mssql_exec, table_facts

VALUE_STATISTICS = ("most_common_vals", "most_common_freqs", "most_common_elem",
                    "histogram_bounds", "elem_count_histogram", "SHOW_STATISTICS",
                    "histogram")


def _queries(module, prefix=""):
    return {name: value for name, value in vars(module).items()
            if name.startswith(prefix) and name.endswith("_SQL") and isinstance(value, str)}


def test_no_postgres_query_reads_a_value_statistic():
    queries = _queries(table_facts)
    assert len(queries) >= 5
    for name, sql in queries.items():
        for word in VALUE_STATISTICS:
            assert word.lower() not in sql.lower(), (name, word)


def test_no_sql_server_query_reads_a_histogram():
    queries = _queries(mssql_exec, "_FACTS_")
    assert len(queries) == 4
    for name, sql in queries.items():
        for word in VALUE_STATISTICS:
            assert word.lower() not in sql.lower(), (name, word)


def test_the_column_statistics_select_names_four_numbers():
    sql = table_facts._COLUMN_STATS_SQL
    select = sql[sql.index("s.attname,"):sql.index("FROM")]
    assert [c.strip() for c in select.split(",")] == [
        "s.attname", "s.null_frac", "s.n_distinct", "s.avg_width", "s.correlation"]


class _Cur:
    """Answers each query of pg_facts in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sql = []
        self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = self.answers.pop(0)

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last)


AT = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def test_pg_facts_maps_the_rows():
    cur = _Cur([
        [(16384,)],
        [("audit_orders", "CREATE TRIGGER audit_orders AFTER INSERT ON public.orders ...", "O"),
         ("off_trigger", "CREATE TRIGGER off_trigger ...", "D")],
        [("email", 0.0, -1.0, 24, 0.123456789), ("state", 0.25, 3.0, 4, None)],
        [(1200, 7, 3, 40, 1300, 20, 100, None, AT, AT, None)],
        [("orders_pkey", 40, 8192)],
    ])
    out = table_facts.pg_facts(cur, "public", "Order Lines")
    assert cur.sql[0] == (table_facts._OID_SQL, ('public."Order Lines"',))
    assert cur.sql[2][1] == ("public", "Order Lines")
    assert out["triggers"] == [
        {"name": "audit_orders", "def": "CREATE TRIGGER audit_orders AFTER INSERT ON public.orders ...",
         "enabled": True},
        {"name": "off_trigger", "def": "CREATE TRIGGER off_trigger ...", "enabled": False}]
    assert out["columnStats"][0] == {"column": "email", "nullFraction": 0.0, "distinct": -1.0,
                                     "avgWidth": 24, "correlation": 0.1235}
    assert out["columnStats"][1]["correlation"] is None
    assert out["activity"]["liveRows"] == 1200 and out["activity"]["lastAutoVacuum"] == AT.isoformat()
    assert out["activity"]["lastVacuum"] is None
    assert out["indexUse"] == [{"name": "orders_pkey", "scans": 40, "bytes": 8192}]


def test_pg_facts_is_none_for_a_missing_relation():
    cur = _Cur([[(None,)]])
    assert table_facts.pg_facts(cur, "public", "gone") is None
    assert len(cur.sql) == 1


def test_a_view_has_no_activity_row():
    cur = _Cur([[(16390,)], [], [], [], []])
    out = table_facts.pg_facts(cur, "public", "big_orders")
    assert out == {"triggers": [], "columnStats": [], "activity": None, "indexUse": []}
