"""schema_catalog + schema_browser — pure formatting/parsing (no DB)."""
from dba_slack_bot import schema_catalog as sc
from dba_slack_bot.slack_app import schema_browser as sb
from dba_slack_bot.slack_app import subcommands as sub


def test_postgres_is_hidden_everywhere():
    # `postgres` (and rdsadmin) must never be snapshotted or listed on any
    # target — the snapshot SQL and every DB-listing read filter on this.
    assert "postgres" in sc._HIDDEN_DATABASES
    assert "rdsadmin" in sc._HIDDEN_DATABASES


def _t(**kw):
    base = {
        "schema_name": "public", "table_name": "orders",
        "relkind": "table", "row_estimate": 1200, "total_bytes": 4096,
        "partition_count": None, "partition_key": None,
        "indexes": None, "foreign_keys": None,
    }
    base.update(kw)
    return base


# --- table_summary_line -------------------------------------------------------

def test_summary_plain_table():
    line = sc.table_summary_line(_t())
    assert "~1.2K rows" in line and "4.0 KB" in line


def test_summary_partitioned_collapses_to_one_line():
    line = sc.table_summary_line(_t(
        relkind="partitioned", row_estimate=7_300_000_000,
        total_bytes=3.6 * 1024**4, partition_count=128,
        partition_key="HASH (user_id)"))
    assert "~7.3B rows" in line
    assert "128 partitions" in line
    assert "hash (user_id)" in line


def test_summary_view_shows_kind_not_size():
    assert sc.table_summary_line(_t(relkind="view", row_estimate=None,
                                    total_bytes=None)) == "view"


# --- format_columns -----------------------------------------------------------

def test_format_columns_markers_and_alignment():
    cols = [
        {"column_name": "id", "data_type": "bigint", "not_null": True,
         "is_pk": True, "in_index": True, "default_expr": None, "ordinal": 1},
        {"column_name": "status", "data_type": "text", "not_null": True,
         "is_pk": False, "in_index": True, "default_expr": None, "ordinal": 2},
        {"column_name": "note", "data_type": "text", "not_null": False,
         "is_pk": False, "in_index": False, "default_expr": None, "ordinal": 3},
    ]
    out = sc.format_columns(cols).split("\n")
    assert out[0].endswith("PK")            # PK wins, no NN/idx noise
    assert out[1].endswith("NN idx")
    assert out[2].rstrip().endswith("text")  # no markers
    # Aligned: the type column starts at the same offset on every line.
    assert out[0].index("bigint") == out[1].index("text")


def test_format_columns_empty():
    assert sc.format_columns([]) == "(no columns)"


# --- index / fk lines ----------------------------------------------------------

def test_format_indexes_extracts_cols():
    line = sc.format_indexes([
        {"name": "pk_orders",
         "def": "CREATE UNIQUE INDEX pk_orders ON public.orders USING btree (id)"},
        {"name": "ix_user",
         "def": "CREATE INDEX ix_user ON public.orders USING btree (user_id, status)"},
    ])
    assert line == "pk_orders(id) · ix_user(user_id, status)"


def test_format_indexes_none():
    assert sc.format_indexes(None) is None
    assert sc.format_indexes([]) is None


# --- browser blocks -------------------------------------------------------------

def test_table_option_value_is_qualified_name():
    opt = sb.table_option(_t())
    assert opt["value"] == "public.orders"
    assert opt["text"]["text"].startswith("public.orders")
    assert len(opt["text"]["text"]) <= 75


def test_code_chunks_respect_slack_limit():
    text = "\n".join(f"col_{i:03d}  text" for i in range(400))
    blocks = sb._code_chunks(text)
    assert len(blocks) >= 2
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks)


def test_browser_modal_carries_scope_and_preselect():
    view = sb.browser_modal(target_id=7, target_alias="t-alias",
                            database="appdb", selected_table="public.orders")
    assert '"t": 7' in view["private_metadata"]
    assert '"d": "appdb"' in view["private_metadata"]
    tbl_input = next(b for b in view["blocks"] if b.get("block_id") == sb.B_TABLE)
    assert tbl_input["dispatch_action"] is True
    assert tbl_input["element"]["initial_option"]["value"] == "public.orders"
    # Read-only reference: no submit button.
    assert "submit" not in view


# --- subcommand arg parsing ------------------------------------------------------

def test_split_target_db():
    assert sub._split_target_db("alias/appdb") == ("alias", "appdb")
    assert sub._split_target_db("alias") == ("alias", None)
    assert sub._split_target_db("alias/") == ("alias", None)
