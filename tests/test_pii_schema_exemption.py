"""A schema-scoped, super-admin-only masking exemption.

The masking catalog matches column NAMES, so the operator's own `dba.*`
monitoring views come back mangled: a view exposing query text, host names or
session owners trips the same rules a customer table does. The values are
infrastructure telemetry and the reader is the person who installed them.

Two dimensions carry that, and the tests here exist because each one has a way
to go wrong that would be silent and serious:

  * **schema** — the existing table rules match the BARE name, so scoping by
    table would make `dba.blocking_sessions` and `public.blocking_sessions` the
    same string and unmask a business table that happens to share a name.
  * **reader** — an exemption is otherwise fleet-wide for everyone, and
    `pg_stat_statements` holds query literals.

And the sharpest edge: a schema row has `table_name IS NULL` and
`column_name IS NULL`, exactly like a database-wide row. If the db-wide branch
does not also require `schema_name IS NULL`, "the dba schema for one reader"
silently becomes "this entire database for that reader".
"""
import pytest

from queryhub import pii

DBA_ROW = {"database_name": None, "schema_name": "dba", "table_name": None,
           "column_name": None, "apply_in_joins": False,
           "keep_value_scan": False, "super_admin_only": True}


def _load(rows):
    return lambda *a, **k: list(rows)


# ---------------------------------------------------------------------------
# the sharpest edge first
# ---------------------------------------------------------------------------

def test_a_schema_row_is_not_treated_as_database_wide(monkeypatch):
    """The whole risk in one test. A dba-schema row must not lift masking on a
    query that never mentions the dba schema."""
    monkeypatch.setattr(pii, "_load_exemptions", _load([DBA_ROW]))
    skip_all, cols = pii.exemption_decision(
        1, "app", "SELECT full_name FROM public.users", ["full_name"],
        principal_id="U_SUPER")
    assert skip_all is False, (
        "a schema-scoped row was read as database-wide — masking just came off "
        "an ordinary business table")
    assert cols == set()


def test_the_exemption_applies_when_every_table_is_in_that_schema(monkeypatch):
    monkeypatch.setattr(pii, "_load_exemptions", _load([DBA_ROW]))
    skip_all, _ = pii.exemption_decision(
        1, "app", "SELECT * FROM dba.blocking_sessions", ["query", "usename"],
        principal_id="U_SUPER")
    assert skip_all is True


def test_a_join_out_of_the_schema_keeps_masking_on(monkeypatch):
    """Same rule joins already follow: one table from anywhere else and the
    whole result stays masked, because provenance per column is not tracked."""
    monkeypatch.setattr(pii, "_load_exemptions", _load([DBA_ROW]))
    skip_all, _ = pii.exemption_decision(
        1, "app",
        "SELECT u.full_name, d.query FROM public.users u "
        "JOIN dba.blocking_sessions d ON true",
        ["full_name", "query"], principal_id="U_SUPER")
    assert skip_all is False


# ---------------------------------------------------------------------------
# fail-closed on names whose schema cannot be known
# ---------------------------------------------------------------------------

def test_an_unqualified_table_never_earns_the_exemption(monkeypatch):
    """`SELECT * FROM blocking_sessions` might resolve to dba via search_path —
    or to public. The text does not say, so masking stays on."""
    monkeypatch.setattr(pii, "_load_exemptions", _load([DBA_ROW]))
    skip_all, _ = pii.exemption_decision(
        1, "app", "SELECT * FROM blocking_sessions", ["query"],
        principal_id="U_SUPER")
    assert skip_all is False


def test_unparseable_sql_never_earns_the_exemption(monkeypatch):
    monkeypatch.setattr(pii, "_load_exemptions", _load([DBA_ROW]))
    skip_all, _ = pii.exemption_decision(
        1, "app", "SELECT * FROM dba.x WHERE 'unbalanced", ["query"],
        principal_id="U_SUPER")
    assert skip_all is False


# ---------------------------------------------------------------------------
# the reader dimension
# ---------------------------------------------------------------------------

def test_only_super_admins_get_it(monkeypatch):
    """The same query, the same row, two readers, two answers."""
    import queryhub.admins as admins_mod
    import queryhub.db as db_mod
    monkeypatch.setattr(db_mod, "fetch_all", lambda *a, **k: [dict(DBA_ROW)])
    monkeypatch.setattr(admins_mod, "is_super_admin", lambda uid: uid == "U_SUPER")

    sql, cols = "SELECT * FROM dba.blocking_sessions", ["query"]
    assert pii.exemption_decision(1, "app", sql, cols,
                                  principal_id="U_SUPER")[0] is True
    assert pii.exemption_decision(1, "app", sql, cols,
                                  principal_id="U_PLAIN")[0] is False


def test_an_unknown_reader_gets_the_strict_answer(monkeypatch):
    """A caller that cannot say who is asking must not receive the privileged
    answer by default."""
    import queryhub.admins as admins_mod
    import queryhub.db as db_mod
    monkeypatch.setattr(db_mod, "fetch_all", lambda *a, **k: [dict(DBA_ROW)])
    monkeypatch.setattr(admins_mod, "is_super_admin",
                        lambda uid: pytest.fail("looked up an absent reader"))
    skip_all, _ = pii.exemption_decision(
        1, "app", "SELECT * FROM dba.blocking_sessions", ["query"])
    assert skip_all is False


# ---------------------------------------------------------------------------
# the loader itself
# ---------------------------------------------------------------------------

def test_loader_drops_privileged_rows_for_a_plain_reader(monkeypatch):
    import queryhub.admins as admins_mod
    import queryhub.db as db_mod
    rows = [dict(DBA_ROW), {"database_name": None, "schema_name": None,
                            "table_name": "public_data", "column_name": None,
                            "apply_in_joins": False, "keep_value_scan": False,
                            "super_admin_only": False}]
    monkeypatch.setattr(db_mod, "fetch_all", lambda *a, **k: rows)
    monkeypatch.setattr(admins_mod, "is_super_admin", lambda uid: False)
    out = pii._load_exemptions(1, "app", "U_PLAIN")
    assert [r["table_name"] for r in out] == ["public_data"], (
        "a super-admin-only row survived for an ordinary reader")


def test_loader_fails_closed_when_the_admin_lookup_raises(monkeypatch):
    import queryhub.admins as admins_mod
    import queryhub.db as db_mod

    def _boom(_uid):
        raise RuntimeError("metadata DB down")

    monkeypatch.setattr(db_mod, "fetch_all", lambda *a, **k: [dict(DBA_ROW)])
    monkeypatch.setattr(admins_mod, "is_super_admin", _boom)
    assert pii._load_exemptions(1, "app", "U_SUPER") == [], (
        "an unavailable admin lookup granted the privileged exemption")


# ---------------------------------------------------------------------------
# the callers must pass the reader, or the whole dimension is decorative
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module,attr", [
    ("queryhub.executor", "_execute_main_statement"),
])
def test_the_executor_passes_the_requester(module, attr):
    import importlib
    import inspect
    src = inspect.getsource(getattr(importlib.import_module(module), attr))
    assert "principal_id=requester_id" in src, (
        "the executor resolves exemptions without saying who is reading, so a "
        "super-admin-scoped row can never apply")


def test_the_web_result_endpoint_passes_the_requester():
    import inspect

    from queryhub.web import routes_queries
    src = inspect.getsource(routes_queries._masked_pii_cols)
    assert 'principal_id=row["requester_slack_id"]' in src, (
        "the header-dot hint would disagree with the delivered file for a "
        "super-admin, which is worse than either answer alone")


# ---------------------------------------------------------------------------
# the mirror of the sharpest edge: a schema that is NOT the scope
# ---------------------------------------------------------------------------
#
# The test above asks whether a schema row is read as database-wide. This one
# asks the opposite question, and it was live for four weeks: a COLUMN row also
# carries `schema_name`, as a narrowing field, and the schema branch read it as
# if the row were schema-scoped. One narrow exemption on
# `public.reward_campaigns.name` therefore lifted masking for every statement
# whose tables were all written `public.x`.
#
# It reached production because the only way to write these rows was psql,
# where nobody fills in a schema they do not need. The Add-exemption form makes
# schema REQUIRED on the column rung, so every exemption written from the
# screen would have carried one.

COL_ROW_WITH_SCHEMA = {"database_name": "app", "schema_name": "public",
                       "table_name": "reward_campaigns", "column_name": "name",
                       "apply_in_joins": False, "keep_value_scan": False,
                       "super_admin_only": False}

TABLE_ROW_WITH_SCHEMA = {"database_name": "app", "schema_name": "public",
                         "table_name": "reward_campaigns", "column_name": None,
                         "apply_in_joins": False, "keep_value_scan": False,
                         "super_admin_only": False}


def test_a_column_rows_schema_does_not_exempt_the_schema(monkeypatch):
    """One column exemption must not unmask an unrelated table in the same
    schema, however the query qualifies its names."""
    monkeypatch.setattr(pii, "_load_exemptions", _load([COL_ROW_WITH_SCHEMA]))
    skip_all, cols = pii.exemption_decision(
        1, "app", "SELECT email, phone FROM public.users", ["email", "phone"])
    assert skip_all is False
    assert cols == set()


def test_a_table_rows_schema_does_not_exempt_the_schema(monkeypatch):
    """Same for the table rung — it narrows the table, it does not widen to
    everything beside it."""
    monkeypatch.setattr(pii, "_load_exemptions", _load([TABLE_ROW_WITH_SCHEMA]))
    skip_all, _ = pii.exemption_decision(
        1, "app", "SELECT email FROM public.users", ["email"])
    assert skip_all is False


def test_the_column_row_still_fires_on_its_own_column(monkeypatch):
    """The fix must not cost the row its actual job."""
    monkeypatch.setattr(pii, "_load_exemptions", _load([COL_ROW_WITH_SCHEMA]))
    skip_all, cols = pii.exemption_decision(
        1, "app", "SELECT name FROM public.reward_campaigns", ["name"])
    assert skip_all is False
    assert cols == {0}


def test_a_real_schema_row_beside_a_column_row_still_works(monkeypatch):
    """Both kinds in scope at once: the schema row keeps its reach and the
    column row keeps its narrowness."""
    monkeypatch.setattr(
        pii, "_load_exemptions", _load([DBA_ROW, COL_ROW_WITH_SCHEMA]))
    skip_all, _ = pii.exemption_decision(
        1, "app", "SELECT query FROM dba.blocking_sessions", ["query"],
        principal_id="U_SUPER")
    assert skip_all is True
    skip_all, _ = pii.exemption_decision(
        1, "app", "SELECT email FROM public.users", ["email"],
        principal_id="U_SUPER")
    assert skip_all is False


# ---------------------------------------------------------------------------
# a schema on a narrow row NARROWS it
# ---------------------------------------------------------------------------
#
# The other half of the same confusion. Once a column row's schema stopped
# being read as "the whole schema is exempt", it was being read as nothing at
# all: `table_name` matches the BARE name, so an exemption for
# `venue.venues.description` also freed `other.venues.description` — the
# same-name collision the schema dimension exists to prevent, arriving through
# the rung nobody was checking.
#
# The rule can only ever refuse. An unqualified statement is admitted, because
# its schema is genuinely unknowable from the text and refusing there would
# start MASKING data that comes back unmasked today, for every query that does
# not bother to qualify.

VENUE_ROW = {"database_name": "app", "schema_name": "venue",
             "table_name": "venues", "column_name": "description",
             "apply_in_joins": True, "keep_value_scan": False,
             "super_admin_only": False}

NO_SCHEMA_ROW = {**VENUE_ROW, "schema_name": None}


def _fires(row, sql):
    monkey = [row]
    return bool(pii._column_skips(
        monkey, pii._tables_in(sql), ["description"], pii._table_refs_in(sql)))


def test_the_same_name_in_another_schema_stays_masked():
    """The leak. Without this, one exemption covers every `venues` in the
    database."""
    assert _fires(VENUE_ROW, "SELECT description FROM other.venues") is False


def test_the_named_schema_still_fires():
    assert _fires(VENUE_ROW, "SELECT description FROM venue.venues") is True


def test_an_unqualified_statement_is_unchanged():
    """The no-regression case, and the reason this is not fail-closed: most
    real queries do not qualify, and refusing there would mask data that comes
    back unmasked today."""
    assert _fires(VENUE_ROW, "SELECT description FROM venues") is True


def test_a_row_with_no_schema_is_untouched():
    """26 of the 30 live rows. They must keep matching any schema."""
    assert _fires(NO_SCHEMA_ROW, "SELECT description FROM other.venues") is True
    assert _fires(NO_SCHEMA_ROW, "SELECT description FROM venues") is True


def test_unparseable_sql_keeps_failing_closed():
    assert _fires(VENUE_ROW, "SELECT description FROM (((") is False


def test_a_table_the_statement_never_names_is_not_second_guessed():
    """The row's table is absent from the statement, so the schema check has
    nothing to say — the table-match rule above it already decided."""
    assert pii._schema_admits(VENUE_ROW, {("public", "orders")}) is True


def test_both_schemas_at_once_still_fires_and_that_is_documented():
    """Known limit, asserted so it is a decision and not a surprise: one of the
    two references does name the exempt schema, and which one the output column
    came from is a lineage question, not a set question."""
    assert _fires(
        VENUE_ROW,
        "SELECT v.description FROM other.venues v JOIN venue.venues w ON 1=1"
    ) is True
