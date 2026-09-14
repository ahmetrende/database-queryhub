"""Batch modal default-fill behavior.

Users almost always run every item in a batch against the same server and
database, so `build_batch_modal` pre-fills the target + database dropdowns
of freshly added / untouched items from the first item that has a target.
A per-item override must never be overwritten, and query text / result
format stay per-item.
"""
from queryhub.slack_app import modal


def _block(view: dict, block_id: str) -> dict | None:
    return next((b for b in view["blocks"] if b.get("block_id") == block_id), None)


def _server_initial(view: dict, index: int) -> dict | None:
    b = _block(view, f"{modal.BATCH_B_SERVER}_{index}")
    return b["element"].get("initial_option") if b else None


def _database_initial(view: dict, index: int) -> dict | None:
    b = _block(view, f"{modal.BATCH_B_DATABASE}_{index}")
    return b["element"].get("initial_option") if b else None


def _query_initial(view: dict, index: int) -> str | None:
    b = _block(view, f"{modal.BATCH_B_QUERY}_{index}")
    return b["element"].get("initial_value") if b else None


def _item1() -> dict:
    return {
        "target_server_id": 23,
        "target_alias": "alpha-prod",
        "target_host_port": "alpha.example.com:5432",
        "database_name": "orders",
        "query": "SELECT 1",
        "wants_result": True,
        "result_format": "csv",
    }


def test_untouched_item_inherits_first_target_and_db():
    view = modal.build_batch_modal(
        item_count=2,
        items_state=[_item1(), {}],
        principal_id=None,
    )
    srv = _server_initial(view, 2)
    dbo = _database_initial(view, 2)
    assert srv is not None and srv["value"] == "23"
    assert srv["text"]["text"] == "alpha-prod"
    assert dbo is not None and dbo["value"] == "orders"


def test_inheritance_does_not_copy_query_text():
    view = modal.build_batch_modal(
        item_count=2,
        items_state=[_item1(), {}],
        principal_id=None,
    )
    # Only target + database are inherited; the added row starts with an
    # empty query so the user must write it (and submit still requires it).
    assert _query_initial(view, 2) is None


def test_per_item_target_override_is_preserved():
    own = {
        "target_server_id": 99,
        "target_alias": "beta-prod",
        "target_host_port": "beta.example.com:5432",
        "database_name": "billing",
        "query": "SELECT 2",
    }
    view = modal.build_batch_modal(
        item_count=2,
        items_state=[_item1(), own],
        principal_id=None,
    )
    srv = _server_initial(view, 2)
    dbo = _database_initial(view, 2)
    assert srv["value"] == "99"          # not overwritten with 23
    assert dbo["value"] == "billing"     # keeps its own db


def test_default_is_encoded_into_private_metadata():
    view = modal.build_batch_modal(
        item_count=2,
        items_state=[_item1(), {}],
        principal_id=None,
    )
    pm = modal.decode_pm(view["private_metadata"])
    assert pm["n"] == 2
    # The inherited target/db is cached in pm so the db typeahead salt and
    # host:port context survive the next views.update round-trip.
    assert pm["items"][1]["tid"] == 23
    assert pm["items"][1]["db"] == "orders"
    assert pm["items"][1]["thp"] == "alpha.example.com:5432"


def test_no_target_anywhere_leaves_items_empty():
    view = modal.build_batch_modal(
        item_count=2,
        items_state=[{}, {}],
        principal_id=None,
    )
    assert _server_initial(view, 1) is None
    assert _server_initial(view, 2) is None


def test_first_item_untouched_second_selected_backfills_first():
    # If the first row with a target is item #2, item #1 (still empty)
    # inherits it — "the first item that has a selection is the template".
    own = {
        "target_server_id": 99,
        "target_alias": "beta-prod",
        "target_host_port": "beta.example.com:5432",
        "database_name": "billing",
    }
    view = modal.build_batch_modal(
        item_count=2,
        items_state=[{}, own],
        principal_id=None,
    )
    assert _server_initial(view, 1)["value"] == "99"
    assert _database_initial(view, 1)["value"] == "billing"
