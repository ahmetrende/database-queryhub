"""pii_masking_exemptions — scope resolution (DB / table / column) + fail-closed.

_load_exemptions is monkeypatched so the decision logic is tested without a
DB. Row shape mirrors the query: {database_name, table_name, column_name}
(database filtering already happened in SQL, so rows here are post-filter).
"""

from queryhub import pii


def _patch(monkeypatch, rows):
    monkeypatch.setattr(pii, "_load_exemptions", lambda tid, db: rows)


def decide(sql, columns, rows, monkeypatch):
    _patch(monkeypatch, rows)
    return pii.exemption_decision(29, "sanctions_service", sql, columns)


# --- _tables_in --------------------------------------------------------------

def test_tables_in_simple():
    assert pii._tables_in("SELECT uid, name FROM sanctions LIMIT 10") == {"sanctions"}


def test_tables_in_join():
    assert pii._tables_in(
        "SELECT s.name FROM sanctions s JOIN users u ON u.id = s.uid"
    ) == {"sanctions", "users"}


def test_tables_in_excludes_cte_alias():
    t = pii._tables_in(
        "WITH x AS (SELECT * FROM sanctions) SELECT * FROM x"
    )
    assert t == {"sanctions"}


def test_tables_in_unparseable_is_none():
    assert pii._tables_in("SELECT FROM WHERE )( nonsense") is None


# --- DB-wide exemption -------------------------------------------------------

def test_db_wide_row_lifts_all(monkeypatch):
    rows = [{"database_name": "sanctions_service", "table_name": None, "column_name": None}]
    skip_all, cols = decide("SELECT name FROM anything", ["name"], rows, monkeypatch)
    assert skip_all is True and cols == set()


def test_no_rows_no_exemption(monkeypatch):
    skip_all, cols = decide("SELECT name FROM sanctions", ["name"], [], monkeypatch)
    assert skip_all is False and cols == set()


# --- table-level -------------------------------------------------------------

TBL = [{"database_name": "sanctions_service", "table_name": "sanctions", "column_name": None}]


def test_table_exempt_single_table(monkeypatch):
    skip_all, _ = decide("SELECT uid, name FROM sanctions LIMIT 10",
                         ["uid", "name"], TBL, monkeypatch)
    assert skip_all is True


def test_table_exempt_join_with_nonexempt_keeps_masking(monkeypatch):
    # Riding a PII table along an exempt one must NOT lift masking.
    skip_all, cols = decide(
        "SELECT u.name FROM sanctions s JOIN users u ON u.id = s.uid",
        ["name"], TBL, monkeypatch)
    assert skip_all is False and cols == set()


def test_table_exempt_unparseable_sql_fails_closed(monkeypatch):
    skip_all, cols = decide("SELECT FROM WHERE )(", ["name"], TBL, monkeypatch)
    assert skip_all is False and cols == set()


def test_table_exempt_cte_over_exempt_table(monkeypatch):
    skip_all, _ = decide(
        "WITH x AS (SELECT * FROM sanctions) SELECT name FROM x",
        ["name"], TBL, monkeypatch)
    assert skip_all is True


# --- column-level ------------------------------------------------------------

def test_column_exempt_unscoped(monkeypatch):
    rows = [{"database_name": "sanctions_service", "table_name": None, "column_name": "name"}]
    skip_all, cols = decide("SELECT uid, name FROM sanctions",
                            ["uid", "name"], rows, monkeypatch)
    assert skip_all is False
    assert cols == {1}


def test_column_exempt_table_scoped_matches_single_table(monkeypatch):
    rows = [{"database_name": "sanctions_service", "table_name": "sanctions", "column_name": "name"}]
    skip_all, cols = decide("SELECT uid, name FROM sanctions",
                            ["uid", "name"], rows, monkeypatch)
    assert cols == {1}


def test_column_exempt_table_scoped_blocked_on_join(monkeypatch):
    rows = [{"database_name": "sanctions_service", "table_name": "sanctions", "column_name": "name"}]
    skip_all, cols = decide(
        "SELECT u.name FROM sanctions s JOIN users u ON u.id = s.uid",
        ["name"], rows, monkeypatch)
    assert cols == set()


def test_column_match_case_insensitive(monkeypatch):
    rows = [{"database_name": None, "table_name": None, "column_name": "NAME"}]
    _, cols = decide("SELECT Name FROM t", ["Name"], rows, monkeypatch)
    assert cols == {0}


# --- mask_row skip_cols ------------------------------------------------------

def test_mask_row_skip_cols_passthrough():
    found = set()
    # Column 0 exempted: even a value the content scanner would mask
    # (an email) passes through untouched.
    out = pii.mask_row(("a@b.com", "x@y.com"), found, {}, skip_cols={0})
    assert out[0] == "a@b.com"
    assert out[1] == "x***@y.com"
    assert "email" in found


def test_mask_row_skip_beats_column_catalog():
    found = set()
    out = pii.mask_row(("Jane Doe",), found, {0: "name"}, skip_cols={0})
    assert out[0] == "Jane Doe"
    assert found == set()
