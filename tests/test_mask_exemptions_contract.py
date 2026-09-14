"""The masking-exemptions screen and the route behind it must agree.

The screen is design-owned and the route is code-owned, so nothing in either
file can be wrong on its own — the mismatch only exists between them. A field
the screen reads and the route never sends renders as `undefined`, and on this
particular screen an `undefined` is a sentence about production data that is
missing its subject: "Unmasks .. in  on ." reads like a rendering bug when it
is actually a route that forgot a column.

These tests are static on purpose. They read the two sources and compare them,
so they hold without a database and they keep holding after a design round
adds a field.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from queryhub import pii
from queryhub.web import routes_admin

ROOT = Path(__file__).resolve().parents[1]
SCREEN = ROOT / "QueryHubWeb" / "qh-admin-mask.jsx"
ROUTE = ROOT / "src" / "queryhub" / "web" / "routes_admin.py"

# `e` is the row in this screen's components AND the event in its handlers.
# These are the event/error members, so a name arriving here that is not a row
# field must be added — the test failing on an unfamiliar `e.x` is the safe
# direction, because the other direction silently stops checking.
_NOT_ROW_FIELDS = {"preventDefault", "target", "code", "message", "g"}


def _screen_row_fields() -> set[str]:
    src = SCREEN.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", " ", src, flags=re.M)
    return {m.group(1) for m in re.finditer(r"\be\.([A-Za-z_][A-Za-z0-9_]*)", src)} \
        - _NOT_ROW_FIELDS


def _route_row_keys() -> set[str]:
    """The keys of the dict `admin_mask_exemptions` appends per row."""
    src = ROUTE.read_text(encoding="utf-8")
    i = src.index("def admin_mask_exemptions(")
    body = src[i:src.index("\n@router", i)]
    j = body.index("out.append({")
    return set(re.findall(r'"([a-zA-Z]+)":', body[j:body.index("})", j)]))


def test_route_sends_every_field_the_screen_reads():
    missing = sorted(_screen_row_fields() - _route_row_keys())
    assert not missing, (
        "the masking-exemptions screen reads fields the route never sends: "
        + ", ".join(missing))


def test_meta_fields_the_screen_reads_are_sent():
    src = SCREEN.read_text(encoding="utf-8")
    reads = set(re.findall(r"\bmeta\.([A-Za-z]+)", src))
    src_route = ROUTE.read_text(encoding="utf-8")
    i = src_route.index("def admin_mask_exemptions(")
    body = src_route[i:src_route.index("\n@router", i)]
    sent = set(re.findall(r'"([a-zA-Z]+)":', body[body.index("return {"):]))
    assert not sorted(reads - sent), sorted(reads - sent)


@pytest.mark.parametrize(("row", "want"), [
    ({"column_name": "address", "table_name": "t", "schema_name": "s",
      "database_name": "d"}, "column"),
    ({"column_name": None, "table_name": "t", "schema_name": "s",
      "database_name": "d"}, "table"),
    ({"column_name": None, "table_name": None, "schema_name": "s",
      "database_name": "d"}, "schema"),
    ({"column_name": None, "table_name": None, "schema_name": None,
      "database_name": "d"}, "database"),
    ({"column_name": None, "table_name": None, "schema_name": None,
      "database_name": None}, "server"),
])
def test_scope_is_the_narrowest_field_named(row, want):
    assert routes_admin._mask_scope(row) == want


def test_a_wildcard_is_named_not_left_blank():
    """A NULL target or database means "all of them" in this table. The route
    has to say so: a blank server on a row that reaches every server is the
    exact reading error the screen's wide rungs exist to prevent."""
    src = ROUTE.read_text(encoding="utf-8")
    i = src.index("def admin_mask_exemptions(")
    body = src[i:src.index("\n@router", i)]
    assert '"every server"' in body and '"every database"' in body


def test_coverage_predicate_reads_null_as_a_wildcard():
    row = {"target_server_id": None, "database_name": None,
           "table_name": "addresses", "column_name": "address"}
    assert routes_admin._mask_covers(row, 7, "anything", "addresses", "address")
    assert not routes_admin._mask_covers(row, 7, "anything", "addresses", "city")
    narrow = {"target_server_id": 7, "database_name": "d",
              "table_name": "addresses", "column_name": "address"}
    assert routes_admin._mask_covers(narrow, 7, "d", "addresses", "address")
    assert not routes_admin._mask_covers(narrow, 8, "d", "addresses", "address")


def test_the_explainer_uses_the_maskers_own_matcher():
    """`explain_columns` names the rule that fired. It must be the SAME match
    the masker makes, or the screen explains a decision no one made."""
    patterns = [("name", "name", "token", ("database", "table")),
                ("email", "email", "substring", ()),
                ("address", "address", "token", ())]
    for name in ("full_name", "database_name", "user_email", "address", "id"):
        entry = pii.match_column_rule(name, patterns)
        assert pii._match_pii_type(name, patterns) == (entry[1] if entry else None)


def test_full_versus_partial_follows_the_masker_not_a_second_table(monkeypatch):
    monkeypatch.setattr(pii, "_load_column_patterns", lambda: [
        ("address", "address", "token", ()),
        ("email", "email", "substring", ()),
    ])
    got = pii.explain_columns(["home_address", "user_email", "id"])
    assert got["home_address"]["mask"] == "full"
    assert got["user_email"]["mask"] == "partial"
    assert got["user_email"]["key"] == "email"
    assert "id" not in got            # unmatched names are absent, not null
    # `_redact` replaces the whole cell; everything else keeps a shape. The
    # screen says "fully" or "partially" masked from this one bit.
    for ptype, want in (("address", "full"), ("generic", "full"),
                        ("email", "partial"), ("name", "partial")):
        masker = pii._COLUMN_MASKERS.get(ptype)
        assert ("full" if masker in (None, pii._redact) else "partial") == want
