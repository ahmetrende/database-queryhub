"""favorites — pure helpers + the modal wiring for the favorite checkbox.

The DB-backed CRUD (add/search/delete) is exercised against a live DB in
manual smoke tests; here we cover the pure pieces and the modal parse so a
silent wiring regression (e.g. the checkbox flag dropping out of
parse_submission) fails loudly.
"""
from dba_slack_bot import favorites
from dba_slack_bot.slack_app import modal


# --- favorites.preview (pure) ----------------------------------------------

def test_preview_collapses_whitespace():
    assert favorites.preview("SELECT   a,\n  b\tFROM t") == "SELECT a, b FROM t"


def test_preview_caps_length():
    long = "SELECT " + "x" * 200
    assert len(favorites.preview(long)) == 60


def test_preview_empty():
    assert favorites.preview("") == ""
    assert favorites.preview(None) == ""


def test_has_per_user_cap():
    assert isinstance(favorites.MAX_PER_USER, int) and favorites.MAX_PER_USER > 0


# --- modal: favorite checkbox parses ---------------------------------------
#
# build_modal() touches the DB (auto-approve banner) so it's out of scope for
# this DB-free suite; parse_submission is pure and locks the wiring that
# actually matters — the favorite flag surviving into the parsed dict.

def _state(favorite: bool) -> dict:
    return {"state": {"values": {
        modal.B_SERVER: {modal.A_SERVER: {"selected_option": {"value": "1"}}},
        modal.B_DATABASE: {modal.A_DATABASE: {"selected_option": {"value": "slackbot"}}},
        modal.B_QUERY: {modal.A_QUERY: {"value": "SELECT 1"}},
        modal.B_JUSTIFICATION: {modal.A_JUSTIFICATION: {"value": ""}},
        modal.B_WANTS_RESULT: {modal.A_WANTS_RESULT: {"selected_option": {"value": "csv"}}},
        modal.B_SAVE_FAVORITE: {modal.A_SAVE_FAVORITE: {
            "selected_options": ([{"value": "favorite"}] if favorite else [])}},
    }}}


def test_parse_submission_reads_favorite_checked():
    assert modal.parse_submission(_state(True))["favorite"] is True


def test_parse_submission_reads_favorite_unchecked():
    assert modal.parse_submission(_state(False))["favorite"] is False
