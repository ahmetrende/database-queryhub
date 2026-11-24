"""db_scope.catalog_database_stats: the numbers under a Connections row."""
from datetime import datetime, timezone

from queryhub import db, db_scope

AT = datetime(2026, 11, 25, tzinfo=timezone.utc)


def test_stats_per_database_skip_hidden_and_keep_unknown_sizes(monkeypatch):
    hidden = next(iter(db_scope.HIDDEN_DATABASES))
    rows = [{"t": 7, "d": "ledger", "n": 3, "rows": 120, "bytes": 4096, "at": AT},
            {"t": 7, "d": hidden, "n": 9, "rows": 1, "bytes": 1, "at": AT},
            {"t": 8, "d": "shop", "n": 1, "rows": None, "bytes": None, "at": AT}]
    seen = {}
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None: seen.update(p=params) or rows)
    got = db_scope.catalog_database_stats([7, 8, 7])
    assert seen["p"] == ([7, 8],)
    assert got == {7: {"ledger": {"tables": 3, "rows": 120, "bytes": 4096, "syncedAt": AT}},
                   8: {"shop": {"tables": 1, "rows": None, "bytes": None, "syncedAt": AT}}}


def test_no_ids_reads_nothing(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("read the database")
    monkeypatch.setattr(db, "fetch_all", refuse)
    assert db_scope.catalog_database_stats([]) == {}
