"""Duplicate result-column names must not collapse the JSON rows."""
from queryhub.web.routes_queries import _dedup_cols


def test_no_duplicates_unchanged():
    assert _dedup_cols(["id", "name", "email"]) == ["id", "name", "email"]


def test_duplicates_get_suffixes():
    # `SELECT a.id, b.id` → both survive as distinct keys.
    assert _dedup_cols(["id", "id"]) == ["id", "id (2)"]
    assert _dedup_cols(["id", "name", "id", "id"]) == \
        ["id", "name", "id (2)", "id (3)"]


def test_rows_keep_both_columns():
    # The actual bug: dict(zip(cols, rec)) dropped the first same-named column.
    cols = _dedup_cols(["id", "id"])
    row = dict(zip(cols, ["1", "2"]))
    assert row == {"id": "1", "id (2)": "2"}
    assert len(row) == 2
