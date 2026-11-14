"""Live facts about one PostgreSQL table that the hourly catalog does not keep.

The schema tools (`schema_tools.table_detail`) add these to the catalog row
of a table: the triggers, the numeric statistics of each column, the
activity counters of the table and the use of each index.

Statistics give NUMBERS only. `pg_stats` also holds `most_common_vals`,
`most_common_freqs`, `histogram_bounds` and `most_common_elems`. Those hold
real values of the column (or the frequencies that go with them), so no
query here names them. `tests/test_table_facts.py` checks the SQL text.

Every read takes a cursor of a READ ONLY transaction, which the caller opens.
"""
from __future__ import annotations

from . import pg_ddl

_OID_SQL = "SELECT pg_catalog.to_regclass(%s)::oid"

_TRIGGERS_SQL = (
    "SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled "
    "  FROM pg_catalog.pg_trigger t "
    " WHERE t.tgrelid = %s AND NOT t.tgisinternal "
    " ORDER BY t.tgname")

# One row per column. A partitioned parent can have two rows for a column
# (`inherited` true and false). The row over the whole tree comes first.
_COLUMN_STATS_SQL = (
    "SELECT DISTINCT ON (s.attname) "
    "       s.attname, s.null_frac, s.n_distinct, s.avg_width, s.correlation "
    "  FROM pg_catalog.pg_stats s "
    " WHERE s.schemaname = %s AND s.tablename = %s "
    " ORDER BY s.attname, s.inherited DESC")

_ACTIVITY_SQL = (
    "SELECT n_live_tup, n_dead_tup, seq_scan, idx_scan, n_tup_ins, n_tup_upd, "
    "       n_tup_del, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze "
    "  FROM pg_catalog.pg_stat_all_tables "
    " WHERE relid = %s")

_INDEX_USE_SQL = (
    "SELECT indexrelname, idx_scan, pg_catalog.pg_relation_size(indexrelid) "
    "  FROM pg_catalog.pg_stat_all_indexes "
    " WHERE relid = %s "
    " ORDER BY indexrelname")

_ENABLED = {"O": True, "A": True, "R": True, "D": False}


def _iso(at) -> str | None:
    return at.isoformat() if at is not None else None


def _num(value):
    """A float as a JSON number with few digits, or None."""
    if value is None:
        return None
    return round(float(value), 4)


def pg_facts(cur, schema: str, table: str) -> dict | None:
    """Triggers, column statistics, activity and index use of `schema.table`.

    `schema` and `table` are the exact names (from the catalog), not quoted.
    None when the server has no such relation.
    """
    ref = pg_ddl.quote_ident(schema) + "." + pg_ddl.quote_ident(table)
    cur.execute(_OID_SQL, (ref,))
    row = cur.fetchone()
    oid = row[0] if row else None
    if oid is None:
        return None

    cur.execute(_TRIGGERS_SQL, (oid,))
    triggers = [{"name": name, "def": definition, "enabled": _ENABLED.get(flag, True)}
                for name, definition, flag in cur.fetchall()]

    cur.execute(_COLUMN_STATS_SQL, (schema, table))
    stats = [{"column": col, "nullFraction": _num(nulls), "distinct": _num(distinct),
              "avgWidth": width, "correlation": _num(corr)}
             for col, nulls, distinct, width, corr in cur.fetchall()]

    cur.execute(_ACTIVITY_SQL, (oid,))
    act = cur.fetchone()
    activity = None
    if act is not None:
        activity = {
            "liveRows": act[0], "deadRows": act[1], "seqScans": act[2],
            "indexScans": act[3], "inserts": act[4], "updates": act[5],
            "deletes": act[6], "lastVacuum": _iso(act[7]),
            "lastAutoVacuum": _iso(act[8]), "lastAnalyze": _iso(act[9]),
            "lastAutoAnalyze": _iso(act[10]),
        }

    cur.execute(_INDEX_USE_SQL, (oid,))
    index_use = [{"name": name, "scans": scans, "bytes": size}
                 for name, scans, size in cur.fetchall()]

    return {"triggers": triggers, "columnStats": stats,
            "activity": activity, "indexUse": index_use}
