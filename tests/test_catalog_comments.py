"""Table and column comments in the schema catalog (P2.4).

PostgreSQL comments and SQL Server MS_Description properties go into the
hourly catalog. The web schema route sends them to a user who passes its grant
check. These tests pin four things:

  * the catalog SQL reads the comment on both engines;
  * the clean helper strips, blanks and caps a comment;
  * the diff writes the comment and counts a comment change as a change;
  * the schema route sends `comment` only when there is one.

No test opens a database. The cursors and the db calls are fakes.
"""
from __future__ import annotations

import pytest

from queryhub import mssql_exec, schema_catalog
from queryhub.web import routes_data


def _flat(sql: str) -> str:
    return " ".join(sql.split())


class RecordingCursor:
    """Captures every statement. `returning` feeds the fetch of the table
    upsert."""

    def __init__(self, returning=None):
        self.sql: list[str] = []
        self.params: list = []
        self._returning = returning or []

    def execute(self, sql, params=None):
        self.sql.append(_flat(sql))
        self.params.append(params)

    def fetchall(self):
        return list(self._returning)


# ---------------------------------------------------------------------------
# the catalog SQL
# ---------------------------------------------------------------------------

def test_the_postgres_table_sql_reads_the_table_comment():
    assert "obj_description(c.oid, 'pg_class') AS comment" in _flat(
        schema_catalog._TABLES_SQL)


def test_the_postgres_column_sql_reads_the_column_comment():
    assert "col_description(a.attrelid, a.attnum) AS comment" in _flat(
        schema_catalog._COLUMNS_SQL)


def test_the_mssql_table_sql_reads_ms_description_of_the_object():
    sql = _flat(mssql_exec._CATALOG_TABLES_SQL)
    assert "LEFT JOIN sys.extended_properties ep" in sql
    assert "ep.name = 'MS_Description'" in sql
    assert "ep.minor_id = 0" in sql
    assert "ep.class = 1" in sql
    assert "CAST(ep.value AS nvarchar(4000)) AS comment" in sql


def test_the_mssql_column_sql_reads_ms_description_of_the_column():
    sql = _flat(mssql_exec._CATALOG_COLUMNS_SQL)
    assert "LEFT JOIN sys.extended_properties ep" in sql
    assert "ep.name = 'MS_Description'" in sql
    assert "ep.minor_id = c.column_id" in sql
    assert "CAST(ep.value AS nvarchar(4000)) AS comment" in sql


# ---------------------------------------------------------------------------
# the clean helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "", "   ", "\n\t"])
def test_an_empty_comment_is_stored_as_null(raw):
    assert schema_catalog.clean_comment(raw) is None


def test_a_comment_is_stripped():
    assert schema_catalog.clean_comment("  Customer orders.  ") == "Customer orders."


def test_a_long_comment_is_cut_and_ends_with_an_ellipsis():
    out = schema_catalog.clean_comment("x" * 5000)
    assert len(out) == schema_catalog.COMMENT_MAX_CHARS
    assert out.endswith("…")


def test_a_comment_at_the_limit_is_kept_whole():
    text = "y" * schema_catalog.COMMENT_MAX_CHARS
    assert schema_catalog.clean_comment(text) == text


# ---------------------------------------------------------------------------
# the diff writes
# ---------------------------------------------------------------------------

def _table(**extra):
    row = {"schema_name": "public", "table_name": "orders", "relkind": "r",
           "row_estimate": 10, "total_bytes": 100, "partition_count": None,
           "partition_key": None, "indexes": None, "foreign_keys": None}
    row.update(extra)
    return row


def test_the_table_upsert_writes_the_cleaned_comment():
    cur = RecordingCursor([{"id": 1, "schema_name": "public",
                            "table_name": "orders"}])
    schema_catalog._sync_tables(cur, 7, "app", [_table(comment="  Orders.  ")])
    upsert = cur.sql[0]
    assert upsert.startswith("INSERT INTO schema_tables")
    assert "comment = EXCLUDED.comment" in upsert
    # target id, database, then one array per field: the comment is the last.
    assert cur.params[0][-1] == ["Orders."]


def test_a_table_dict_without_a_comment_key_still_works():
    """ClickHouse and Athena build their own dicts and send no comment."""
    cur = RecordingCursor([{"id": 1, "schema_name": "public",
                            "table_name": "orders"}])
    assert schema_catalog._sync_tables(cur, 7, "app", [_table()]) == {
        ("public", "orders"): 1}
    assert cur.params[0][-1] == [None]


def test_the_column_upsert_writes_the_comment_and_diffs_on_it():
    cur = RecordingCursor()
    schema_catalog._sync_columns(cur, [1], [
        (1, 1, "id", "bigint", True, None, True, True, "Order id.")])
    upsert = next(s for s in cur.sql if s.startswith("INSERT INTO schema_columns"))
    assert "comment = EXCLUDED.comment" in upsert
    guard = upsert[upsert.index(" WHERE ("):]
    assert "schema_columns.comment" in guard
    assert "EXCLUDED.comment" in guard
    assert cur.params[0][-1] == ["Order id."]


class FakeColumnStore:
    """Just enough of `schema_columns` to count writes, with the comment."""

    def __init__(self, rows):
        self.rows = dict(rows)
        self.writes = 0

    def execute(self, sql, params=None):
        s = _flat(sql)
        if s.startswith("INSERT INTO schema_columns"):
            tids, ords, names, types, nn, dflt, pk, inidx, cm = params
            for i, name in enumerate(names):
                new = (ords[i], types[i], nn[i], dflt[i], pk[i], inidx[i], cm[i])
                if self.rows.get((tids[i], name)) != new:
                    self.rows[(tids[i], name)] = new
                    self.writes += 1
        elif s.startswith("DELETE FROM schema_columns sc"):
            _ids, tids, names = params
            keep = set(zip(tids, names))
            for key in [k for k in self.rows if k not in keep]:
                del self.rows[key]
                self.writes += 1


def test_a_changed_comment_is_a_change_and_an_unchanged_one_is_not():
    stored = FakeColumnStore({(1, "id"): (1, "bigint", True, None, True, True,
                                          "Old text.")})
    want = [(1, 1, "id", "bigint", True, None, True, True, "New text.")]
    schema_catalog._sync_columns(stored, [1], want)
    assert stored.writes == 1
    assert stored.rows[(1, "id")][-1] == "New text."
    schema_catalog._sync_columns(stored, [1], want)
    assert stored.writes == 1, "an unchanged comment must not write again"


def test_snapshot_reads_the_comment_from_engine_dicts(monkeypatch):
    """The column tuple carries the cleaned comment. A dict without the key
    (ClickHouse style) gives None."""
    columns = [
        {"schema_name": "public", "table_name": "orders", "ordinal": 1,
         "column_name": "id", "data_type": "bigint", "not_null": True,
         "default_expr": None, "is_pk": True, "in_index": True,
         "comment": " Order id. "},
        {"schema_name": "public", "table_name": "orders", "ordinal": 2,
         "column_name": "note", "data_type": "text", "not_null": False,
         "default_expr": None, "is_pk": False, "in_index": False},
    ]

    class Target:
        id = 7
        alias = "test-target"
        engine = "clickhouse"
        host = "db.example.invalid"
        port = 9440
        username = "reader"

    from queryhub import clickhouse_exec
    monkeypatch.setattr(clickhouse_exec, "catalog_snapshot",
                        lambda *a, **k: ([_table()], columns))
    monkeypatch.setattr(schema_catalog, "_routines", lambda *a, **k: [])
    monkeypatch.setattr(schema_catalog, "_sync_tables",
                        lambda cur, tid, dbn, tables: {("public", "orders"): 1})
    seen = {}
    monkeypatch.setattr(schema_catalog, "_sync_columns",
                        lambda cur, ids, rows: seen.setdefault("rows", rows))
    monkeypatch.setattr(schema_catalog, "_sync_routines", lambda *a, **k: None)

    class Tx:
        def __enter__(self):
            return RecordingCursor()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(schema_catalog.db, "transaction", lambda: Tx())
    schema_catalog.snapshot_database(Target(), "pw", "app")
    assert [r[-1] for r in seen["rows"]] == ["Order id.", None]


# ---------------------------------------------------------------------------
# the schema route
# ---------------------------------------------------------------------------

def _schema_payload(monkeypatch, trows, crows):
    class Target:
        id = 7

    monkeypatch.setattr(routes_data.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(routes_data, "_granted_db",
                        lambda uid, alias, dbname: (Target(), object()))
    monkeypatch.setattr(routes_data.schema_catalog, "catalog_stamp",
                        lambda tid, dbn: {"snapshotAt": None, "sig": "s"})

    def fetch_all(sql, params=None):
        return trows if "FROM schema_tables WHERE" in sql else crows

    monkeypatch.setattr(routes_data.db, "fetch_all", fetch_all)
    from queryhub import pii
    monkeypatch.setattr(pii, "column_pii_map", lambda names: [])
    return routes_data.db_schema("test-conn", "app", claims={"sub": "U_TEST"})


def test_the_schema_route_selects_the_comments():
    import inspect
    src = inspect.getsource(routes_data.db_schema)
    assert "foreign_keys, comment" in src
    assert "c.comment" in src


def test_the_schema_route_sends_a_comment_only_when_present(monkeypatch):
    trows = [
        {"id": 1, "schema_name": "public", "table_name": "orders",
         "relkind": "table", "row_estimate": 10, "indexes": None,
         "foreign_keys": None, "comment": "Customer orders."},
        {"id": 2, "schema_name": "public", "table_name": "items",
         "relkind": "table", "row_estimate": 5, "indexes": None,
         "foreign_keys": None, "comment": None},
        {"id": 3, "schema_name": "public", "table_name": "open_orders",
         "relkind": "view", "row_estimate": None, "indexes": None,
         "foreign_keys": None, "comment": "Orders not shipped."},
    ]
    crows = [
        {"table_id": 1, "column_name": "id", "data_type": "bigint",
         "not_null": True, "is_pk": True, "comment": "Order id."},
        {"table_id": 1, "column_name": "note", "data_type": "text",
         "not_null": False, "is_pk": False, "comment": None},
        {"table_id": 2, "column_name": "sku", "data_type": "text",
         "not_null": True, "is_pk": False, "comment": ""},
    ]
    out = _schema_payload(monkeypatch, trows, crows)
    by_name = {t["name"]: t for t in out["tables"]}
    assert by_name["orders"]["comment"] == "Customer orders."
    assert "comment" not in by_name["items"]
    cols = {c["name"]: c for c in by_name["orders"]["columns"]}
    assert cols["id"]["comment"] == "Order id."
    assert "comment" not in cols["note"]
    assert "comment" not in by_name["items"]["columns"][0]
    assert out["views"][0]["comment"] == "Orders not shipped."
    # The existing keys stay.
    assert set(cols["note"]) == {"name", "type", "nullable", "pk", "pii"}
    assert set(by_name["items"]) == {"name", "schema", "approxRows", "columns",
                                     "indexes"}


def test_comments_stay_out_of_the_ai_and_mcp_schema_tools():
    """A comment can hold sensitive text, and the AI sends table details to a
    model provider. schema_tools must not read the comment column."""
    import inspect

    from queryhub import schema_tools
    assert "comment" not in inspect.getsource(schema_tools).lower()
