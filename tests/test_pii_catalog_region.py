"""The default PII catalog must not corrupt a stranger's ordinary data.

The seeded catalog mixed unambiguous English patterns with Turkish ones, and the
Turkish tokens are everyday words elsewhere: `ad` is an advertisement, `cep` a
region code, `pan` a camera movement. Measured on a 27-name sample before this
change, 23 innocent columns were flagged — `product_name` came back as
`W***** M****`, `ad_id` as `A******`, and `cep` as the empty string. It errs
safe rather than leaking, but a tool that corrupts a new user's first query is
one they uninstall.

Two mechanisms, both measured against 99,479 real column names catalogued from
the live fleet before being chosen:

  region          Turkish tokens fire only when pii_region = 'tr'. Cheap to
                  decide: tokens 'ad', 'pan', 'tel' and 'cep' occur ZERO times
                  in that fleet, so region-gating them costs nothing.
  exclude_tokens  the broad `name` token skips database metadata. It occurs
                  6,989 times, and the top names are database_name (1913),
                  schema_name (773), table_name (673), host_name,
                  application_name, program_name, index_name and object_name,
                  against full_name at 56. A SQL gateway reads more metadata
                  than anything else.

Result, re-measured after both migrations: generic flags 0 of 40 innocent names
and 17 of 17 real PII ones; tr flags 7 of 40 (all Turkish tokens) and 31 of 31.
"""
import pytest

from dba_slack_bot import pii


# The matcher is pure, so these exercise it with catalog rows directly rather
# than needing the database. Shape: (pattern, pii_type, match_type, excludes).
GENERIC = [
    ("full_name", "name", "substring", ()),
    ("first_name", "name", "substring", ()),
    ("email", "email", "token", ()),
    ("email_address", "email", "substring", ()),
    ("home_address", "address", "substring", ()),
    ("phone", "phone", "token", ()),
    ("name", "name", "token", ("database", "db", "schema", "table", "column",
                               "index", "object", "host", "server", "file",
                               "application", "program", "service", "product",
                               "category", "brand", "country", "status",
                               "event", "display", "slot", "parameter")),
    ("addr", "address", "substring", ("ip", "mac", "host", "contract",
                                      "wallet", "check")),
    ("birth", "birthdate", "substring", ("certificate", "template", "country",
                                         "place")),
]

INNOCENT = [
    "product_name", "category_name", "country_name", "status_name",
    "event_name", "file_name", "table_name", "column_name", "db_name",
    "display_name", "database_name", "schema_name", "host_name",
    "application_name", "program_name", "index_name", "object_name",
    "slot_name", "server_name", "brand_name", "parameter_name",
    "ip_address", "mac_address", "contract_address", "wallet_address",
    "address_line_check", "birth_certificate_template", "birth_country",
]

REAL_PII = [
    "full_name", "first_name", "email", "email_address", "user_email",
    "home_address", "phone", "customer_full_name",
]


@pytest.mark.parametrize("column", INNOCENT)
def test_no_ordinary_column_is_flagged(column):
    assert pii._match_pii_type(column, GENERIC) is None, \
        f"{column} would be corrupted on a fresh install"


@pytest.mark.parametrize("column", REAL_PII)
def test_real_pii_is_still_flagged(column):
    assert pii._match_pii_type(column, GENERIC) is not None, \
        f"{column} stopped being masked"


def test_a_bare_name_column_still_matches():
    """`name` in an application table usually IS a person, and it occurs 836
    times in the measured fleet. Exclusion is about QUALIFIERS, so a name with
    no qualifier must behave exactly as it did before."""
    assert pii._match_pii_type("name", GENERIC) == "name"


def test_exclusion_narrows_and_never_widens():
    """The safety property: adding excludes to a pattern can only ever turn a
    match into a non-match. Anything the pattern did NOT match before still does
    not match, and any name without a qualifier keeps its old verdict."""
    bare = [(p, t, m, ()) for p, t, m, _ in GENERIC]
    for col in ["name", "full_name", "email", "home_address", "birthdate",
                "unrelated_column", "id", "created_at"]:
        with_ex = pii._match_pii_type(col, GENERIC)
        without = pii._match_pii_type(col, bare)
        assert with_ex in (without, None), col


def test_a_pattern_with_no_excludes_behaves_as_before():
    """Backward compatibility for a row that predates the column: a 3-tuple has
    to keep working, because that is what the fallback loader returns on an
    install that has not applied migration 088."""
    old_style = [("email", "email", "token"), ("full_name", "name", "substring")]
    assert pii._match_pii_type("email", old_style) == "email"
    assert pii._match_pii_type("user_full_name", old_style) == "name"
    assert pii._match_pii_type("product_name", old_style) is None


def test_the_first_matching_pattern_wins_and_an_excluded_one_does_not_stop_it():
    """A column excluded by one pattern must still be checked against the rest —
    otherwise an exclusion on a broad rule could suppress a precise rule that
    follows it."""
    patterns = [
        ("name", "name", "token", ("customer",)),        # excluded here
        ("full_name", "name", "substring", ()),          # but caught here
    ]
    assert pii._match_pii_type("customer_full_name", patterns) == "name"


def test_the_loader_filters_by_region(monkeypatch):
    """Language-specific rows must not load outside their region — that is the
    whole mechanism, and it lives in SQL rather than in Python."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src"
           / "dba_slack_bot" / "pii.py").read_text(encoding="utf-8")
    assert "AND (region IS NULL OR lower(region) = %s)" in src
    assert "(region(),)" in src


def test_the_loader_survives_a_database_without_the_new_columns(monkeypatch):
    """An install that has not run migration 088 must keep masking, not crash
    and not silently mask nothing. It falls back to loading every pattern, which
    is the pre-migration behaviour — never less than before."""
    from dba_slack_bot import db
    calls = []

    def _fetch_all(sql, params=None):
        calls.append(sql)
        if "exclude_tokens" in sql:
            raise RuntimeError('column "region" does not exist')
        return [{"pattern": "email", "pii_type": "email", "match_type": "token"}]

    monkeypatch.setattr(db, "fetch_all", _fetch_all)
    monkeypatch.setattr(pii, "region", lambda: "generic")
    out = pii._load_column_patterns()
    assert len(calls) == 2, "did not retry without the new columns"
    assert out == [("email", "email", "token", ())]


def test_the_loader_normalizes_exclude_tokens(monkeypatch):
    """Excludes are compared against a lowercased name, so a row entered in
    mixed case has to be folded or it would never match."""
    from dba_slack_bot import db
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None: [
        {"pattern": "Name", "pii_type": "name", "match_type": "token",
         "exclude_tokens": ["Database", "TABLE"]}])
    monkeypatch.setattr(pii, "region", lambda: "generic")
    pats = pii._load_column_patterns()
    assert pats == [("name", "name", "token", ("database", "table"))]
    assert pii._match_pii_type("database_name", pats) is None
    assert pii._match_pii_type("name", pats) == "name"


def test_a_null_exclude_column_is_an_empty_tuple(monkeypatch):
    from dba_slack_bot import db
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None: [
        {"pattern": "email", "pii_type": "email", "match_type": "token",
         "exclude_tokens": None}])
    monkeypatch.setattr(pii, "region", lambda: "generic")
    assert pii._load_column_patterns() == [("email", "email", "token", ())]


def test_both_migrations_are_present_and_ordered():
    """089 is a separate file rather than an edit to 088 on purpose: 088 is
    already applied and the ledger records its checksum, so editing it in place
    would report every later run as a modified migration."""
    import pathlib
    mig = pathlib.Path(__file__).resolve().parent.parent / "migrations"
    a = mig / "088_pii_pattern_region_and_excludes.sql"
    b = mig / "089_pii_pattern_broad_tokens.sql"
    assert a.exists() and b.exists()
    assert "ADD COLUMN IF NOT EXISTS region" in a.read_text()
    assert "ADD COLUMN IF NOT EXISTS exclude_tokens" in a.read_text()
    assert "SET enabled = FALSE" in b.read_text()
