"""PII content detectors are region packs, and the default is country-neutral.

Four of the six original detectors only worked in Turkey, and two of them —
an 11-digit national id and a 10-digit tax number — match ANY numeric run of
that length, separated from ordinary data only by a national checksum. Roughly
a tenth of arbitrary 10-digit values pass the tax-number checksum, so on a
non-Turkish deployment the same column would both leak (an unformatted local
phone number is not detected) and get mangled (a tenth of its rows come back
as "masked tax numbers", with the audit trail claiming a tax number was
protected).

`pii_region` now selects the pack; `generic` is the default.
"""
import pytest

from dba_slack_bot import config as cfg
from dba_slack_bot import pii


@pytest.fixture
def region(monkeypatch):
    def _set(name):
        monkeypatch.setattr(
            cfg, "get_setting",
            lambda k, d=None: name if k == "pii_region" else d)
    return _set


def _scan(value):
    found = set()
    return pii.mask_value(value, found), found


def test_generic_is_the_default(monkeypatch):
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: d)
    assert pii.region() == "generic"


def test_unknown_region_falls_back_to_generic(region):
    region("atlantis")
    assert pii.region() == "generic"
    # and it still masks — falling back must not mean "mask nothing"
    out, found = _scan("jane@example.com")
    assert found == {"email"}


@pytest.mark.parametrize("value", [
    "4155550123",        # unformatted US phone: 10 digits
    "1234567890",        # arbitrary 10-digit identifier
    "12345678901",       # arbitrary 11-digit identifier
])
def test_generic_pack_leaves_bare_digit_runs_alone(region, value):
    region("generic")
    out, found = _scan(value)
    assert out == value and not found, "bare digit run was mangled"


@pytest.mark.parametrize("value,kind", [
    ("DE89370400440532013000", "iban"),      # Germany
    ("GB33BUKB20201555555555", "iban"),      # UK
    ("TR330006100519786457841326", "iban"),  # Turkey still works in generic
    ("4532015112830366", "card"),            # Luhn-valid Visa
    ("jane.doe@example.com", "email"),
    ("+1 415 555 0123", "phone"),            # E.164
])
def test_generic_pack_detects_country_neutral_kinds(region, value, kind):
    region("generic")
    out, found = _scan(value)
    assert kind in found, f"{kind} not detected in {value}"
    assert out != value


def test_iban_validator_rejects_wrong_length_and_checksum(region):
    region("generic")
    # Right shape, wrong checksum -> not masked (no false positives).
    out, found = _scan("DE89370400440532013001")
    assert not found
    # Unknown country code -> not an IBAN.
    assert pii._valid_iban("ZZ89370400440532013000") is False


def test_tr_pack_adds_the_turkish_kinds(region):
    region("tr")
    _, found = _scan("10000000146")          # valid TC kimlik
    assert "tckn" in found
    _, found = _scan("+90 532 123 45 67")
    assert "phone" in found


def test_tr_pack_still_available_after_default_change(region):
    # The live TR deployment sets pii_region=tr explicitly; make sure that
    # keeps the detectors the generic default deliberately drops.
    region("tr")
    names = {d.name for d in pii.active_detectors()}
    assert {"tckn", "vkn"} <= names


@pytest.mark.parametrize("value", ["453212", "12", "1234", "0"])
def test_masks_never_grow_the_value(value):
    # _mask_card('453212') used to return '45323212' — longer than the input,
    # corrupting short columns like card_last4 and revealing extra digits.
    for masker in (pii._mask_card, pii._mask_iban, pii._mask_tckn, pii._mask_vkn):
        out = masker(value)
        assert len(out) <= len(value), f"{masker.__name__} grew {value!r} -> {out!r}"
