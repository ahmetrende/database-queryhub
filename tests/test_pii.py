"""pii — content detectors (checksum-guarded) + column-name catalog masking.

These cases exercise the `tr` region pack, which is the superset: it adds the
TC-kimlik and tax-number detectors on top of the country-neutral kinds. The
default pack (`generic`) — and why those two digit-run detectors are opt-in —
is covered in test_pii_regions.py.
"""
import pytest

from queryhub import config as cfg
from queryhub import pii


@pytest.fixture(autouse=True)
def _tr_region(monkeypatch):
    monkeypatch.setattr(
        cfg, "get_setting",
        lambda k, d=None: "tr" if k == "pii_region" else d)


def mask(value):
    found = set()
    return pii.mask_value(value, found), found


# --- email ------------------------------------------------------------------

def test_email_masked():
    out, found = mask("john.doe@example.com")
    assert out == "j***@example.com"
    assert "email" in found


def test_email_inside_expression_value():
    # Content-based: catches it even when it's part of a larger string.
    out, found = mask("contact: a@b.com")
    assert "a***@b.com" in out
    assert "email" in found


# --- phone (conservative: needs prefix/separators) --------------------------

def test_phone_with_separators_masked():
    out, found = mask("+90 532 111 22 33")
    assert out.endswith("33") and "*" in out
    assert "phone" in found


def test_bare_10_digits_not_phone():
    out, found = mask("5321234567")
    assert out == "5321234567"
    assert "phone" not in found


def test_plain_id_not_masked():
    # 12 digits: too long for VKN(10)/TCKN(11), not a card prefix → untouched.
    out, found = mask("123456789012")
    assert out == "123456789012"
    assert found == set()


# --- TCKN (11-digit + checksum) --------------------------------------------

def _valid_tckn():
    base = [1, 0, 0, 0, 0, 0, 0, 0, 0]
    d10 = ((base[0] + base[2] + base[4] + base[6] + base[8]) * 7
           - (base[1] + base[3] + base[5] + base[7])) % 10
    d11 = (sum(base) + d10) % 10
    return "".join(map(str, base + [d10, d11]))


def test_valid_tckn_masked():
    tc = _valid_tckn()
    out, found = mask(tc)
    assert "tckn" in found
    assert out != tc and out.endswith(tc[-2:])


def test_invalid_tckn_not_masked():
    out, found = mask("12345678901")  # fails checksum
    assert out == "12345678901"
    assert "tckn" not in found


# --- IBAN (mod-97) ----------------------------------------------------------

def test_valid_iban_masked():
    out, found = mask("TR330006100519786457841326")
    assert "iban" in found
    assert out.startswith("TR") and out.endswith("1326")


# --- card (Luhn + network prefix) ------------------------------------------

def test_valid_visa_masked():
    out, found = mask("4242424242424242")
    assert "card" in found
    assert out.startswith("4242") and out.endswith("4242")


def test_luhn_fail_card_not_masked():
    out, found = mask("4242424242424241")  # fails Luhn
    assert out == "4242424242424241"
    assert "card" not in found


def test_non_network_prefix_not_masked():
    out, found = mask("1234567812345670")  # prefix '1' is no card network
    assert "card" not in found


# --- type preservation ------------------------------------------------------

def test_non_strings_untouched():
    from datetime import datetime, timezone
    from decimal import Decimal
    for v in (1, 3.14, True, False, None,
              Decimal("10.5"), datetime(2026, 1, 1, tzinfo=timezone.utc)):
        out, found = mask(v)
        assert out is v
        assert found == set()


def test_amount_not_masked():
    out, found = mask("1500000")
    assert out == "1500000"
    assert found == set()


# --- VKN content detector ---------------------------------------------------

def test_valid_vkn_masked():
    # 1234567890 is VKN-checksum-valid (confirmed against the live module).
    out, found = mask("1234567890")
    assert "vkn" in found
    assert out.startswith("123") and out.endswith("90")


# --- column-name catalog ----------------------------------------------------
#
# column_pii_map reads pii_column_patterns from the DB. We patch the loader
# with a fixed pattern set so the catalog logic (token / substring matching)
# is tested deterministically without a live DB.

import pytest


@pytest.fixture
def fixed_patterns(monkeypatch):
    patterns = [
        ("name", "name", "token"),
        ("email", "email", "token"),
        ("adres", "address", "substring"),
        ("mobile", "phone", "token"),
        ("vergi", "vkn", "token"),
    ]
    monkeypatch.setattr(pii, "_load_column_patterns", lambda: patterns)
    yield


def test_column_pii_map_matches_known_names(fixed_patterns):
    cols = ["id", "full_name", "customer_email", "ev_adresi", "mobile",
            "vergi_no", "amount", "aciklama"]
    cmap = pii.column_pii_map(cols)
    got = {cols[i]: t for i, t in cmap.items()}
    assert got.get("full_name") == "name"          # token match
    assert got.get("customer_email") == "email"    # token match
    assert got.get("ev_adresi") == "address"       # substring 'adres'
    assert got.get("mobile") == "phone"            # token match
    assert got.get("vergi_no") == "vkn"            # token 'vergi'
    assert "id" not in got and "amount" not in got and "aciklama" not in got


def test_mask_row_column_layer_masks_bare_phone(fixed_patterns):
    # A column flagged 'phone' masks a bare digit run the content layer
    # deliberately skips (no prefix/separators).
    cols = ["id", "mobile"]
    cmap = pii.column_pii_map(cols)
    found = set()
    out = pii.mask_row(("abc", "5336330270"), found, cmap)
    assert out[0] == "abc"
    assert out[1].endswith("70") and "*" in out[1]
    assert "phone" in found


def test_mask_row_name_and_address(fixed_patterns):
    cols = ["full_name", "ev_adresi"]
    cmap = pii.column_pii_map(cols)
    found = set()
    out = pii.mask_row(("Ada Lovelace", "123 Example Street"), found, cmap)
    assert out[0] == "A** L*******"
    assert out[1] == "[REDACTED]"


# --- alias / cast can't defeat the by-name catalog -------------------------

def test_alias_bypass_closed_with_source_lineage(fixed_patterns):
    # `full_name AS x` renames the output column to 'x', which the catalog
    # would miss — until we map the position back to its source column.
    cols = ["x", "z"]
    sql = "SELECT full_name AS x, customer_email AS z FROM users"
    # Without the SQL, the alias hides it (this is the bug).
    assert pii.column_pii_map(cols) == {}
    # With the SQL, the source columns are matched.
    cmap = pii.column_pii_map(cols, sql)
    assert cmap.get(0) == "name"
    assert cmap.get(1) == "email"


def test_cast_bypass_closed_with_source_lineage(fixed_patterns):
    # A numeric/text cast + alias also dodges both layers; lineage catches it.
    cols = ["m"]
    cmap = pii.column_pii_map(cols, "SELECT mobile::text AS m FROM t")
    assert cmap.get(0) == "phone"


def test_select_star_falls_back_to_result_names(fixed_patterns):
    # SELECT * can't be expanded without the schema — fall back to the real
    # result-column names (which ARE the true names) with no crash.
    cols = ["id", "full_name"]
    cmap = pii.column_pii_map(cols, "SELECT * FROM users")
    assert cmap.get(1) == "name" and 0 not in cmap


def test_arity_mismatch_falls_back_safely(fixed_patterns):
    # Projection count != result column count → no positional mapping;
    # fall back to result-name matching only (never mis-attribute).
    cmap = pii.column_pii_map(["a"], "SELECT full_name, customer_email FROM t")
    assert cmap == {}


def test_source_columns_by_position_pure():
    # Pure parser helper: maps each output position to its source columns.
    src = pii._source_columns_by_position(
        "SELECT full_name AS x, lower(email) AS e FROM users", ["x", "e"])
    assert src == [{"full_name"}, {"email"}]
    # Star / arity mismatch → None (caller falls back).
    assert pii._source_columns_by_position("SELECT * FROM t", ["a", "b"]) is None
    assert pii._source_columns_by_position("SELECT a, b FROM t", ["only"]) is None
