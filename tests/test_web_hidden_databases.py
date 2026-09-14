"""The control-plane database must not reach a sidebar by any route.

`postgres` is hidden everywhere on purpose: on this fleet it carries the
operator's own `dba.*` monitoring toolkit, and it is `default_database` on almost
every target. Those two facts met in `/connections` and it leaked — a freshly
enabled target has no catalog yet, the empty list triggered the
default-database fallback, and the fallback ran *after* the hidden filter, so
`postgres` went straight to the tree. A real user saw it.

The regression is a filter-ordering mistake, which is why the tests below assert
on order rather than on the final string: the fallback has to be filtered too,
and a target left with nothing after filtering has to disappear instead of
falling back again.
"""
import inspect
import re

from dba_slack_bot.web import routes_data


def test_the_fallback_is_filtered_and_not_the_other_way_round():
    """The hidden-DB filter must come after the default_database fallback.

    Asserted structurally because reaching the real branch needs a target whose
    catalog is empty, a grant, and a live metadata database. The bug was purely
    positional and this pins the position.
    """
    src = inspect.getsource(routes_data.connections)
    fallback = src.index("dbs = [t.default_database]")
    filters = [m.start() for m in
               re.finditer(r"dbs = \[d for d in dbs if d not in _HIDDEN_DATABASES\]", src)]
    assert filters, "the hidden-database filter is gone from /connections"
    assert any(pos > fallback for pos in filters), (
        "every hidden-database filter runs BEFORE the default_database "
        "fallback, so the fallback value is never filtered — this is exactly "
        "the bug that put `postgres` in a user's sidebar")


def test_a_target_with_nothing_left_after_filtering_is_dropped():
    """If filtering empties the list, the target must be skipped — not fall
    back a second time. Otherwise the fix would just move the leak."""
    src = inspect.getsource(routes_data.connections)
    tail = src[src.rindex("dbs = [d for d in dbs if d not in _HIDDEN_DATABASES]"):]
    assert "continue" in tail.split("db_entries")[0], (
        "after the final filter there is no `continue`, so a target whose only "
        "database was hidden still builds an entry")


def test_postgres_is_in_the_hidden_set():
    """The set itself, in case a refactor ever empties it."""
    assert "postgres" in routes_data._HIDDEN_DATABASES


def test_catalog_listing_also_filters():
    """`_catalog_databases` is the other way a hidden name could arrive: a stale
    snapshot row from before the catalog started excluding these."""
    src = inspect.getsource(routes_data._catalog_databases)
    assert "_HIDDEN_DATABASES" in src


def test_single_database_endpoint_refuses_hidden_names():
    """The tree is not the only surface — a caller can name a database
    directly, and that path must refuse rather than serve it."""
    src = inspect.getsource(routes_data)
    assert "if dbname in _HIDDEN_DATABASES" in src, (
        "the per-database endpoint no longer rejects hidden names")
