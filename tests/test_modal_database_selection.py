"""Reading the database select out of a modal that has been re-rendered.

The element's `action_id` is salted with the target (`act_database_v3`) so
Slack's client re-fetches its options after a target switch. `block_id` does
not change, and Slack keeps view state per (block_id, action_id) — so after a
switch `state.values["blk_database"]` can carry TWO entries.

The old reader took `next(k.startswith("act_database"))`, i.e. whichever the
payload happened to serialise first. Two ways that goes wrong, one reported by
a developer and one nobody had hit yet:

  * the pre-target render is picked. Its selection is null, so the submission
    falls back to the target's `default_database`. On the target this was
    reported against that default is a LOG database, while the table the query
    named lives in the service one — so submit-time validation failed with
    `Relation "..." does not exist`. The same query from a favourite worked,
    because that path renders WITH a target from the first frame and never
    grows a second entry.
  * the PREVIOUS target's entry is picked, and its database is submitted
    against the new server.

Both are ordering-dependent, so both tests build the dict in the order that
used to fail and in the order that used to pass.
"""
from __future__ import annotations

import pytest

from queryhub.slack_app import modal

A = modal.A_DATABASE


def _sel(value):
    if value is None:
        return {"type": "external_select", "selected_option": None}
    return {"type": "external_select",
            "selected_option": {"text": {"type": "plain_text", "text": value},
                                "value": value}}


def _db(block):
    return (block.get("selected_option") or {}).get("value")


# ---------------------------------------------------------------------------
# the reported bug
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", ["stale-first", "fresh-first"])
def test_the_pre_target_render_never_wins(order):
    """The entry from before a target was chosen holds no selection. Picking it
    silently means "use the default database", which is a different database on
    every target that has one."""
    entries = [(A, _sel(None)), (f"{A}_v3", _sel("svc_db"))]
    if order == "fresh-first":
        entries.reverse()
    section = dict(entries)
    assert _db(modal.read_db_block(section, A, 3)) == "svc_db"


@pytest.mark.parametrize("order", ["stale-first", "fresh-first"])
def test_another_targets_database_is_never_submitted(order):
    """Pick server A, pick a database on it, switch to server B. B's entry is
    empty; A's still holds a database that does not exist on B — and worse,
    might exist on B and mean something else entirely."""
    entries = [(f"{A}_v3", _sel("svc_db")), (f"{A}_v9", _sel(None))]
    if order == "fresh-first":
        entries.reverse()
    section = dict(entries)
    assert _db(modal.read_db_block(section, A, 9)) is None


def test_the_selection_for_the_chosen_target_is_returned():
    section = {f"{A}_v3": _sel("svc_db"), f"{A}_v9": _sel("other_db")}
    assert _db(modal.read_db_block(section, A, 3)) == "svc_db"
    assert _db(modal.read_db_block(section, A, 9)) == "other_db"


# ---------------------------------------------------------------------------
# the paths that already worked must keep working
# ---------------------------------------------------------------------------

def test_a_modal_rendered_with_a_target_from_the_first_frame():
    """The favourite / template / edit-and-resubmit path: one entry, salted."""
    section = {f"{A}_v3": _sel("svc_db")}
    assert _db(modal.read_db_block(section, A, 3)) == "svc_db"


def test_an_unsalted_entry_is_accepted_before_any_switch():
    """A target picked but the views.update not yet landed — the only entry is
    the unsalted one, and it is legitimate."""
    section = {A: _sel("svc_db")}
    assert _db(modal.read_db_block(section, A, 3)) == "svc_db"


def test_no_target_yet_reads_the_unsalted_entry():
    section = {A: _sel("svc_db")}
    assert _db(modal.read_db_block(section, A, None)) == "svc_db"


def test_nothing_selected_is_none_not_a_crash():
    assert modal.read_db_block({}, A, 3) == {}
    assert _db(modal.read_db_block({}, A, None)) is None


# ---------------------------------------------------------------------------
# the batch modal salts the same way, per item
# ---------------------------------------------------------------------------

def test_the_batch_item_uses_its_own_indexed_key():
    base = f"{modal.BATCH_A_DATABASE}_2"
    section = {base: _sel(None), f"{base}_v7": _sel("other_db")}
    assert _db(modal.read_db_block(section, base, 7)) == "other_db"
