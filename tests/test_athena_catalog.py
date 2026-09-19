"""Athena catalog reads — the Glue shapes, without Glue.

The catalog for this engine is not a query: `information_schema` would cost
money, run slower, and be refused by the engine's own read-only profile, so the
snapshot asks Glue instead. These tests pin the two translations that make that
work, both of which are easy to get subtly wrong and impossible to notice
afterwards:

* a Glue table becomes the same dict the Postgres reader produces, because the
  write phase downstream is shared and knows nothing about engines;
* a partition key becomes a COLUMN. `month` is not a column in Glue's list --
  it is the folder name -- but in Athena it is queryable, and it is the one
  predicate that decides whether a query scans one month or all of them. A
  catalog that hid it would hide the cheapest thing a user can do.
"""
import pytest

from queryhub import athena_exec


class _Target:
    alias = "svc-prod-archive"
    engine = "athena"
    engine_config = {
        "region": "eu-central-1",
        "workgroup": "wg-archive",
        "database": "archive_db",
        "role_arn": "arn:aws:iam::111111111111:role/reader",
    }


_GLUE_TABLE = {
    "Name": "events",
    "TableType": "EXTERNAL_TABLE",
    "Parameters": {"recordCount": "2400810508", "sizeKey": "93449410998"},
    "StorageDescriptor": {"Columns": [
        {"Name": "tx_id", "Type": "string"},
        {"Name": "available", "Type": "decimal(38,18)"},
        {"Name": "event_time", "Type": "timestamp"},
    ]},
    "PartitionKeys": [{"Name": "month", "Type": "string"}],
}


def test_config_names_every_missing_key_at_once():
    """An operator fixing a row should learn everything wrong with it in one
    read, not one key per attempt."""
    class T:
        alias = "svc-prod-archive"
        engine_config = {"region": "eu-central-1"}
    with pytest.raises(athena_exec.AthenaConfigError) as e:
        athena_exec.config_of(T())
    for key in ("workgroup", "database", "role_arn"):
        assert key in str(e.value)
    assert "svc-prod-archive" in str(e.value)


def test_the_catalog_defaults_but_nothing_else_does():
    """`catalog` has one right answer for every AWS account, so it defaults.
    The other four identify THIS archive and must never be guessed."""
    cfg = athena_exec.config_of(_Target())
    assert cfg["catalog"] == "AwsDataCatalog"
    assert cfg["database"] == "archive_db"


def test_a_glue_table_becomes_the_shared_row_shape():
    table, _ = athena_exec._table_rows(_GLUE_TABLE, "archive_db")
    # The product models target -> database -> schema -> table and Athena has
    # no schema level, so the database name fills both. That is what makes the
    # browser's `schema.table` qualification valid Athena SQL.
    assert table["schema_name"] == "archive_db"
    assert table["table_name"] == "events"
    assert table["relkind"] == "r"
    assert table["row_estimate"] == 2400810508
    assert table["total_bytes"] == 93449410998
    assert table["partition_key"] == "month"
    # Glue holds neither, and an empty list would claim we looked.
    assert table["indexes"] is None and table["foreign_keys"] is None


def test_a_view_is_a_view():
    view = dict(_GLUE_TABLE, TableType="VIRTUAL_VIEW")
    table, _ = athena_exec._table_rows(view, "archive_db")
    assert table["relkind"] == "v"


def test_the_partition_key_is_offered_as_a_column():
    """The cheapest predicate on the archive is `month = '...'`. If the catalog
    does not carry it, autocomplete cannot offer it and the browser cannot show
    it — and the user writes the expensive query instead."""
    _, columns = athena_exec._table_rows(_GLUE_TABLE, "archive_db")
    names = [c["column_name"] for c in columns]
    assert names == ["tx_id", "available", "event_time", "month"]
    assert columns[-1]["ordinal"] == 4          # after the stored columns


def test_money_keeps_its_precision_in_the_catalog():
    """`decimal(38,18)` must reach the browser as itself. A column the user
    reads as `double` is a column they will compare with `=` and be wrong."""
    _, columns = athena_exec._table_rows(_GLUE_TABLE, "archive_db")
    assert columns[1]["data_type"] == "decimal(38,18)"


def test_an_unmeasured_table_reports_nothing_rather_than_zero():
    """A crawler that has not measured a table leaves the parameter out. Zero
    would read as "empty table" on the screen, which is a different claim."""
    bare = dict(_GLUE_TABLE, Parameters={})
    table, _ = athena_exec._table_rows(bare, "archive_db")
    assert table["row_estimate"] is None
    assert table["total_bytes"] is None
