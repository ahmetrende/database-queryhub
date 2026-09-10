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

from queryhub.web import deps, routes_admin
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
