"""The control-plane database must not reach a sidebar by any route.

`postgres` is hidden everywhere on purpose: on this fleet it carries the
operator's own `dba.*` monitoring toolkit, and it is `default_database` on almost
every target. Those two facts met in `/connections` and it leaked — a freshly
enabled target has no catalog yet, the empty list triggered the
default-database fallback, and the fallback ran *after* the hidden filter, so
`postgres` went straight to the tree. A real user saw it.

The rule now lives in `db_scope`, shared by the web UI and the MCP tools. The
regression is a filter-ordering mistake: the fallback has to be filtered too,
and a target left with nothing after filtering has to disappear instead of
falling back again. The tests below run the rule, not the source text.
"""
import inspect
from types import SimpleNamespace

from queryhub import db_scope
from queryhub.web import routes_data

UNRESTRICTED = {"mode": "ro", "allowed_databases": None}


def _t(default="postgres"):
    return SimpleNamespace(default_database=default)


def test_the_fallback_is_filtered_and_not_the_other_way_round():
    """An empty catalog falls back to the default database, and the default
    database is `postgres`: the result must be empty, not `['postgres']`."""
    assert db_scope.visible_databases(_t("postgres"), UNRESTRICTED, []) == []


def test_the_fallback_still_serves_a_server_that_has_no_snapshot_yet():
    assert db_scope.visible_databases(_t("ledger"), UNRESTRICTED, []) == ["ledger"]


def test_a_target_with_nothing_left_after_filtering_is_dropped():
    """A restricted grant that lists only hidden databases does not fall back to
    the default database a second time: the person holds no grant on it."""
    grant = {"mode": "ro", "allowed_databases": {"postgres"}}
    assert db_scope.visible_databases(_t("ledger"), grant, ["ledger"]) == []
    empty = {"mode": "ro", "allowed_databases": set()}
    assert db_scope.visible_databases(_t("ledger"), empty, ["ledger"]) == []


def test_postgres_is_in_the_hidden_set():
    """The set itself, in case a refactor ever empties it."""
    assert "postgres" in db_scope.HIDDEN_DATABASES
    assert routes_data._HIDDEN_DATABASES is db_scope.HIDDEN_DATABASES


def test_catalog_listing_also_filters(monkeypatch):
    """The other way a hidden name could arrive: a stale snapshot row from
    before the catalog started excluding these."""
    rows = [{"database_name": "postgres"}, {"database_name": "ledger"}]
    monkeypatch.setattr(db_scope.db, "fetch_all", lambda *a, **k: rows)
    assert db_scope.catalog_databases(1) == ["ledger"]


def test_single_database_endpoint_refuses_hidden_names():
    """The tree is not the only surface — a caller can name a database
    directly, and that path must refuse rather than serve it."""
    assert db_scope.grant_covers(UNRESTRICTED, "ledger") is True
    assert db_scope.grant_covers(UNRESTRICTED, "postgres") is False
    named = {"mode": "ro", "allowed_databases": {"postgres", "ledger"}}
    assert db_scope.grant_covers(named, "postgres") is False
    assert "db_scope.grant_covers" in inspect.getsource(routes_data._granted_db)
