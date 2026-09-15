"""Writing a masking exemption from the web panel.

Every row this path creates removes a protection, so the tests here are about
the two ways that goes wrong QUIETLY:

  * **the stored row is wider than the sentence the operator confirmed.**
    NULL is a wildcard in `pii_masking_exemptions`, so a field left over from
    an earlier state of the form does not add noise, it changes the rung. The
    form clears its fields when the rung changes; the route must not depend on
    that, because a body is not a form.
  * **fleet is inferred rather than declared.** "Every server" and "the server
    dropdown did not populate" arrive as the same empty string. The widest row
    in the table must be reachable only by asking for it.

`_mask_fields` is pure apart from one target lookup, so it is tested directly.
"""
from __future__ import annotations

import pytest

from queryhub.web import routes_admin
from queryhub.web.routes_admin import MaskExemptionIn


@pytest.fixture(autouse=True)
def _known_connection(monkeypatch):
    monkeypatch.setattr(routes_admin, "_target_id_of",
                        lambda alias: 7 if alias == "svc-prod-x" else None)


def _body(**kw):
    base = {"connectionId": "svc-prod-x", "databaseId": "app",
            "schema": "public", "table": "users", "column": "email",
            "reason": "holds a wallet address, not a postal one"}
    base.update(kw)
    return MaskExemptionIn(**base)


def _err(**kw):
    with pytest.raises(Exception) as e:
        routes_admin._mask_fields(_body(**kw))
    return e.value


# ---------------------------------------------------------------------------
# the rung decides the row
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scope,expected", [
    ("column",   {"database_name": "app", "schema_name": "public",
                  "table_name": "users", "column_name": "email"}),
    ("table",    {"database_name": "app", "schema_name": "public",
                  "table_name": "users", "column_name": None}),
    ("schema",   {"database_name": "app", "schema_name": "public",
                  "table_name": None, "column_name": None}),
    ("database", {"database_name": "app", "schema_name": None,
                  "table_name": None, "column_name": None}),
    ("server",   {"database_name": None, "schema_name": None,
                  "table_name": None, "column_name": None}),
])
def test_fields_below_the_rung_are_dropped(scope, expected):
    """The body still carries a table and a column on every one of these. A
    row that kept them would be read back as a narrower rung than the one the
    operator confirmed — and on the wide rungs, the sentence they confirmed is
    the whole safety mechanism."""
    _tid, f = routes_admin._mask_fields(_body(scope=scope))
    for k, v in expected.items():
        assert f[k] == v, f"{scope}: {k}"


def test_the_server_rung_still_names_its_server():
    tid, f = routes_admin._mask_fields(_body(scope="server"))
    assert tid == 7 and f["target_server_id"] == 7


# ---------------------------------------------------------------------------
# fleet is declared, never inferred
# ---------------------------------------------------------------------------

def test_fleet_comes_from_the_scope():
    _tid, f = routes_admin._mask_fields(
        _body(scope="fleet", connectionId="", databaseId="", schema="dba"))
    assert f["target_server_id"] is None
    assert f["database_name"] is None
    assert f["schema_name"] == "dba"


def test_fleet_without_a_schema_is_the_whole_fleet():
    _tid, f = routes_admin._mask_fields(
        _body(scope="fleet", connectionId="", schema=""))
    assert f["target_server_id"] is None and f["schema_name"] is None


def test_a_missing_connection_on_a_narrow_rung_is_refused_not_widened():
    """The bug this test exists for: an empty connection read as 'every
    server' turns a failed dropdown into the widest row in the table."""
    exc = _err(scope="column", connectionId="")
    assert getattr(exc, "status_code", None) == 400


def test_an_unknown_connection_is_refused():
    exc = _err(connectionId="not-a-target")
    assert getattr(exc, "status_code", None) == 404


# ---------------------------------------------------------------------------
# what each rung requires
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scope,missing", [
    ("database", {"databaseId": ""}),
    ("schema",   {"schema": ""}),
    ("table",    {"table": ""}),
    ("column",   {"column": ""}),
])
def test_a_rung_refuses_its_own_missing_field(scope, missing):
    assert getattr(_err(scope=scope, **missing), "status_code", None) == 400


def test_a_reason_is_required():
    assert getattr(_err(reason="   "), "status_code", None) == 400


def test_an_unknown_rung_is_refused():
    assert getattr(_err(scope="everything"), "status_code", None) == 400


# ---------------------------------------------------------------------------
# the three switches
# ---------------------------------------------------------------------------

def test_soft_keeps_the_value_scan_and_full_stops_it():
    _t, soft = routes_admin._mask_fields(_body(strength="soft"))
    _t, full = routes_admin._mask_fields(_body(strength="full"))
    assert soft["keep_value_scan"] is True
    assert full["keep_value_scan"] is False


def test_audience_maps_to_super_admin_only():
    _t, everyone = routes_admin._mask_fields(_body(audience="everyone"))
    _t, sup = routes_admin._mask_fields(_body(audience="super"))
    assert everyone["super_admin_only"] is False
    assert sup["super_admin_only"] is True


@pytest.mark.parametrize("kw", [{"strength": "medium"}, {"audience": "some"}])
def test_an_unknown_switch_value_is_refused_not_defaulted(kw):
    """Defaulting an unrecognised value is how a 'full' exemption becomes a
    'soft' one nobody asked for — or the reverse."""
    assert getattr(_err(**kw), "status_code", None) == 400


# ---------------------------------------------------------------------------
# editing a row in place
# ---------------------------------------------------------------------------
#
# An exemption can be edited for everything EXCEPT what it reaches. That line
# is the whole design: a row's reason describes the row's reach, so a row whose
# reach moved is a row whose reason is now a lie about production data.
# Narrowing stays turn-off-and-create, which leaves two rows each describing
# itself.
#
# The dangerous failure is not refusing an edit -- it is accepting one and not
# applying it. A client that believes it narrowed a row, against a server that
# dropped the field, shows an operator a protection they do not have.

from queryhub.web.routes_admin import MaskPatchIn  # noqa: E402


def _patch(**kw):
    return routes_admin._mask_edit_fields(MaskPatchIn(**kw))


def _patch_err(**kw):
    with pytest.raises(Exception) as e:
        _patch(**kw)
    return e.value


@pytest.mark.parametrize("field,value", [
    ("scope", "database"), ("connectionId", "svc-prod-x"),
    ("databaseId", "app"), ("schema", "public"),
    ("table", "users"), ("column", "email"),
])
def test_a_field_that_moves_the_reach_is_refused_not_ignored(field, value):
    err = _patch_err(**{field: value})
    assert getattr(err, "status_code", None) == 400
    assert getattr(err, "code", None) == "reach_immutable" or \
        "reach_immutable" in str(getattr(err, "detail", ""))


def test_the_refusal_names_which_field_it_refused():
    """An operator who sent two fields and gets "something is fixed" has to
    guess which; the message says both."""
    err = _patch_err(scope="database", table="users")
    text = str(getattr(err, "detail", err))
    assert "scope" in text and "table" in text


def test_an_unknown_field_is_refused_by_the_model():
    """`extra: forbid`. A field this endpoint has never heard of is far more
    likely a client meaning something than a client sending noise."""
    with pytest.raises(Exception):
        MaskPatchIn(enabled=True, reach="everything")


def test_each_switch_maps_to_its_column():
    assert _patch(strength="soft")["keep_value_scan"] is True
    assert _patch(strength="full")["keep_value_scan"] is False
    assert _patch(survivesJoin=True)["apply_in_joins"] is True
    assert _patch(audience="super")["super_admin_only"] is True
    assert _patch(audience="everyone")["super_admin_only"] is False
    assert _patch(enabled=False)["enabled"] is False


def test_an_unset_field_is_untouched_not_defaulted():
    """The difference between "leave it alone" and "set it to the default" is
    a protection switched off by a form that never showed the field."""
    assert _patch(reason="still true, checked again") == {
        "reason": "still true, checked again"}
    assert _patch(enabled=True) == {"enabled": True}
    assert _patch() == {}


def test_a_reason_edited_to_nothing_is_refused():
    """Editing exists largely so a reason can be corrected. A reason that can
    be cleared would make this the one endpoint able to destroy the field it
    was built to repair."""
    err = _patch_err(reason="   ")
    assert getattr(err, "status_code", None) == 400
    assert getattr(err, "code", None) == "reason_required" or \
        "reason_required" in str(getattr(err, "detail", ""))


def test_a_reason_is_stored_trimmed():
    assert _patch(reason="  leading and trailing  ") == {
        "reason": "leading and trailing"}


@pytest.mark.parametrize("kw", [{"strength": "medium"}, {"audience": "some"}])
def test_an_unknown_switch_value_is_refused_on_the_edit_path_too(kw):
    assert getattr(_patch_err(**kw), "status_code", None) == 400


def test_the_row_builder_names_a_wildcard_rather_than_leaving_it_blank():
    """A row that reaches every server must not read as a row with a blank
    server -- on the edit response as much as in the listing, which is why both
    are built here."""
    row = routes_admin._mask_row_core(
        {"id": 1, "target_server_id": None, "database_name": None,
         "schema_name": None, "table_name": None, "column_name": None,
         "reason": "r", "enabled": True, "created_by": "U1",
         "created_at": None, "updated_by": None, "updated_at": None,
         "apply_in_joins": False, "keep_value_scan": False,
         "super_admin_only": False, "alias": None}, {})
    assert row["connectionName"] == "every server"
    assert row["databaseName"] == "every database"
    assert row["scope"] == "fleet"
    assert row["updatedBy"] is None and row["updatedAt"] is None
    assert row["id"] == "1", "ids are strings on this API"


# ---------------------------------------------------------------------------
# the preview's promise
# ---------------------------------------------------------------------------
#
# "Today" and "After" are the two halves of a claim about production data, so
# the only acceptable implementation is the real masker run twice. These tests
# hold it to that: the change must be confined to what the exemption names,
# and a SOFT exemption must still catch a real email in the column it frees.

from queryhub import pii  # noqa: E402


def _pair(monkeypatch, *, stored=(), strength="full",
          columns=("id", "email", "full_name"),
          row=(1, "someone@example.com", "Ayse Yilmaz"),
          column="full_name"):
    monkeypatch.setattr(pii, "_load_exemptions", lambda *a, **k: list(stored))
    monkeypatch.setattr(pii, "is_enabled", lambda: True)
    monkeypatch.setattr(pii, "_load_column_patterns", lambda: [
        ("email", "email", "substring", ()),
        ("name", "name", "substring", ()),
    ])
    return routes_admin._mask_preview_pair(
        7, "app", "public", "users", column, list(columns), list(row),
        strength=strength)


def test_today_masks_what_the_rules_mask(monkeypatch):
    before, _after = _pair(monkeypatch)
    assert before[0] == "1"
    assert before[1] != "someone@example.com"
    assert before[2] != "Ayse Yilmaz"


def test_after_frees_only_the_named_column(monkeypatch):
    before, after = _pair(monkeypatch)
    assert after[2] == "Ayse Yilmaz"        # the exempted column
    assert after[1] == before[1]            # the neighbour stays masked
    assert after[0] == before[0]


def test_a_soft_exemption_still_catches_a_real_email(monkeypatch):
    """Soft drops the NAME rule and keeps the value scan. An exemption on a
    column that turns out to hold an address must not hand it over."""
    _before, after = _pair(
        monkeypatch, strength="soft", column="full_name",
        columns=("id", "full_name"), row=(1, "someone@example.com"))
    assert after[1] != "someone@example.com"


def test_a_full_exemption_hands_over_the_same_value(monkeypatch):
    """The mirror of the test above — and the reason the preview defaults to
    full when the form does not say: it is the wider of the two answers."""
    _before, after = _pair(
        monkeypatch, strength="full", column="full_name",
        columns=("id", "full_name"), row=(1, "someone@example.com"))
    assert after[1] == "someone@example.com"


def test_null_is_named_rather_than_shown_as_a_blank_cell():
    assert routes_admin._preview_cell(None) == "NULL"


def test_a_long_cell_is_truncated_not_dropped():
    out = routes_admin._preview_cell("x" * 500)
    assert len(out) <= 120 and out.endswith("…")


# ---------------------------------------------------------------------------
# the join evidence
# ---------------------------------------------------------------------------
#
# `survivesJoin` is the one setting on the form nobody can answer from the
# schema: it asks whether re-masking on a join makes the exemption useless in
# practice. The answer is how the table is actually queried, and the server is
# the only side that can count it.

def _req(query, engine="postgres"):
    return {"query": query, "engine": engine}


def _stats(monkeypatch, rows, table="orders"):
    monkeypatch.setattr(routes_admin.db, "fetch_all", lambda *a, **k: rows)
    return routes_admin._mask_join_stats(1, "app", table)


def test_a_join_is_counted_by_the_parser_not_by_the_word(monkeypatch):
    """`JOIN` inside a string literal is not a join, and a comma join has no
    JOIN in it at all. Both answers come from the same parser the masker uses
    to decide the exemption, so the evidence and the enforcement cannot
    disagree."""
    got = _stats(monkeypatch, [
        _req("SELECT * FROM orders"),
        _req("SELECT * FROM orders, customers WHERE orders.cid = customers.id"),
        _req("SELECT note FROM orders WHERE note = 'inner join pending'"),
    ])
    assert got == {"total": 3, "joined": 1}


def test_the_table_name_in_a_literal_is_not_a_query_against_it(monkeypatch):
    """The ILIKE is a prefilter, exactly as the evidence lookup treats it."""
    assert _stats(monkeypatch, [
        _req("SELECT * FROM audit WHERE msg = 'see orders'")]) is None


def test_nothing_queried_it_says_nothing(monkeypatch):
    """"0 of 0" is not evidence. None, so the screen prints no sentence rather
    than a sentence with no content in it."""
    assert _stats(monkeypatch, []) is None


def test_a_statement_that_will_not_parse_is_skipped_not_counted(monkeypatch):
    """A denominator that silently includes rows the numerator cannot reach is
    worse than a smaller honest one."""
    got = _stats(monkeypatch, [_req("SELECT * FROM orders"),
                               _req("!! not sql at all")])
    assert got == {"total": 1, "joined": 0}
