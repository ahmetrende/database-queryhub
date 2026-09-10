"""The batched reads behind the connections payload must answer what the
single-target ones answer.

`GET /connections` is the first thing the app loads and it cannot render
anything without it. It used to ask one question per row and then one per
database inside each row: 313 statements and 656ms for a 50-connection reader,
121 for the 117-row admin listing. Four batched reads replaced them.

The risk in that change is not that it is slower — it is that a batch quietly
answers something ELSE: a pair missing from a map reads as "no tables" rather
than as a bug, a cap applied to the whole result instead of per database
truncates the wrong list, and a cross product between two `ANY` lists hands one
target another target's tables. Each test below is one of those.

The grant resolver has its own equivalence test
(`test_effective_grants_batch.py`); these cover the three catalog/auto-approve
reads batched on 2026-09-10.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from queryhub import auto_approve
from queryhub.web import routes_data


# ---------------------------------------------------------------------------
# databases per target
# ---------------------------------------------------------------------------

ROWS = [
    {"t": 1, "d": "ledger"},
    {"t": 1, "d": "postgres"},        # hidden
    {"t": 1, "d": "audit"},
    {"t": 2, "d": "nova"},
    {"t": 3, "d": "postgres"},        # hidden — target 3 ends up absent
]


def test_the_map_hides_the_same_databases_the_single_read_hides(monkeypatch):
    """`_HIDDEN_DATABASES` is read from the module rather than restated, so a
    database added to it is covered here without editing this test."""
    monkeypatch.setattr(routes_data.db, "fetch_all", lambda *a, **k: list(ROWS))
    got = routes_data._catalog_databases_map([1, 2, 3])
    assert got == {1: ["ledger", "audit"], 2: ["nova"]}
    assert 3 not in got, "a target whose only database is hidden must not appear"
    assert "postgres" in routes_data._HIDDEN_DATABASES, (
        "this test's fixture assumes `postgres` is the hidden one")


def test_a_target_with_no_snapshot_is_absent_not_empty(monkeypatch):
    """Absent and [] are the same answer to the caller (`.get(tid, [])`), and
    the map must not invent a key that the query did not return."""
    monkeypatch.setattr(routes_data.db, "fetch_all", lambda *a, **k: [])
    assert routes_data._catalog_databases_map([1, 2]) == {}


def test_no_targets_asks_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("empty input must not reach the database")
    monkeypatch.setattr(routes_data.db, "fetch_all", boom)
    assert routes_data._catalog_databases_map([]) == {}


# ---------------------------------------------------------------------------
# tables per (target, database)
# ---------------------------------------------------------------------------

def _tbl(t, d, schema, name):
    return {"t": t, "d": d, "schema_name": schema, "table_name": name}


def test_the_cross_product_of_two_any_lists_is_discarded(monkeypatch):
    """`WHERE target = ANY(...) AND database = ANY(...)` matches pairs nobody
    asked for — target 2's `ledger` when target 1 has a `ledger` and target 2 a
    `nova`. Handing those to the caller shows one server another server's
    tables."""
    rows = [_tbl(1, "ledger", "public", "a"),
            _tbl(2, "ledger", "public", "SHOULD_NOT_APPEAR"),
            _tbl(2, "nova", "public", "b")]
    monkeypatch.setattr(routes_data.db, "fetch_all", lambda *a, **k: rows)
    got = routes_data._catalog_table_refs_map([(1, "ledger"), (2, "nova")])
    assert got == {(1, "ledger"): [{"s": "public", "n": "a"}],
                   (2, "nova"): [{"s": "public", "n": "b"}]}


def test_the_cap_is_per_database_not_per_result(monkeypatch):
    """Two databases of 3 tables each, cap 3: both keep all three. A cap applied
    to the whole result would empty the second one."""
    monkeypatch.setattr(routes_data, "_max_tables_per_db", lambda: 3)
    rows = ([_tbl(1, "a", "public", f"t{i}") for i in range(3)]
            + [_tbl(1, "b", "public", f"t{i}") for i in range(3)])
    monkeypatch.setattr(routes_data.db, "fetch_all", lambda *a, **k: rows)
    got = routes_data._catalog_table_refs_map([(1, "a"), (1, "b")])
    assert len(got[(1, "a")]) == 3 and len(got[(1, "b")]) == 3


def test_over_the_cap_truncates_that_database_only(monkeypatch):
    monkeypatch.setattr(routes_data, "_max_tables_per_db", lambda: 2)
    rows = ([_tbl(1, "a", "public", f"t{i}") for i in range(5)]
            + [_tbl(1, "b", "public", "only")])
    monkeypatch.setattr(routes_data.db, "fetch_all", lambda *a, **k: rows)
    got = routes_data._catalog_table_refs_map([(1, "a"), (1, "b")])
    assert [r["n"] for r in got[(1, "a")]] == ["t0", "t1"]
    assert [r["n"] for r in got[(1, "b")]] == ["only"]


def test_the_functions_map_fails_open(monkeypatch):
    """A routine catalog that cannot be read costs the reader their
    suggestions. It must never cost them the connection list — which is what an
    exception escaping here would do."""
    monkeypatch.setattr(routes_data.schema_catalog, "catalog_functions_map",
                        lambda pairs: (_ for _ in ()).throw(RuntimeError("gone")))
    assert routes_data._catalog_functions_map([(1, "a")]) == {}


# ---------------------------------------------------------------------------
# auto-approve, loaded once and matched many times
# ---------------------------------------------------------------------------

def _grant(**kw):
    row = {"id": 1, "slack_user_id": "U1", "max_tier": "ro",
           "target_server_id": None, "database_name": None,
           "starts_at": datetime.now(timezone.utc) - timedelta(days=1),
           "expires_at": None, "reason": "r", "granted_by": "U2"}
    row.update(kw)
    return row


@pytest.mark.parametrize("scope,target,database,expect", [
    ({}, 5, "nova", True),                                    # broad grant
    ({"target_server_id": 5}, 5, "nova", True),
    ({"target_server_id": 5}, 6, "nova", False),
    ({"target_server_id": 5, "database_name": "nova"}, 5, "nova", True),
    ({"target_server_id": 5, "database_name": "nova"}, 5, "other", False),
])
def test_passing_the_rows_in_gives_the_same_answer_as_reading_them(
        monkeypatch, scope, target, database, expect):
    """The scope match is pure, so the caller may load the rows once. If these
    two ever disagree, the connections payload starts claiming auto-approve on
    a database the submit path will still queue for a human."""
    rows = [_grant(**scope)]
    monkeypatch.setattr(auto_approve, "active_grants", lambda *a, **k: list(rows))
    read = auto_approve.effective_grant("U1", "ro", target, database)
    passed = auto_approve.effective_grant("U1", "ro", target, database, rows=rows)
    assert (read is not None) is expect
    assert (passed is not None) is expect


def test_passing_rows_in_does_not_read_the_database(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("rows were supplied; nothing should be read")
    monkeypatch.setattr(auto_approve, "active_grants", boom)
    assert auto_approve.effective_grant(
        "U1", "ro", 5, "nova", rows=[_grant()]) is not None


def test_an_empty_row_list_is_not_mistaken_for_no_rows_supplied(monkeypatch):
    """`[]` means "this principal has no live grants", and it must not fall
    through to a read — that is the difference between one query and N."""
    def boom(*a, **k):
        raise AssertionError("[] means no grants, not 'go and look'")
    monkeypatch.setattr(auto_approve, "active_grants", boom)
    assert auto_approve.effective_grant("U1", "ro", 5, "nova", rows=[]) is None
