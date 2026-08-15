"""A cell can be a whole document, and the masker only understood scalars.

`mask_value` opened with `if not isinstance(value, str): return value`. psycopg
returns jsonb as a dict, an array as a list, a composite as a tuple and bytea as
bytes, so every one of those walked straight through. Measured on this
deployment 2026-07-30, all four reached the delivered CSV in clear:

    as_bytea      b'victim@example.com'
    as_jsonb      {'email': 'victim@example.com'}
    as_array      ['victim@example.com']
    as_composite  ('victim@example.com', '1')

The jsonb case is the worst of them: one column can hold every PII kind at
once, and `SELECT payload FROM events` is an ordinary query. Same promise the
EXPLAIN-lineage work defends, through a different hole.

Two properties these tests exist to hold onto:
  - nothing leaks, at any depth, through either masking layer;
  - the CONTAINER TYPE survives, because the CSV writer, the XLSX writer, the
    Slack preview and the web JSON response all still have to work.
"""
import pytest

from dba_slack_bot import pii


SECRETS = ("victim@example.com", "10000000146", "Ada Lovelace", "1 Main St")


def _leaks(value) -> bool:
    return any(s in str(value) for s in SECRETS)


# ---------------------------------------------------------------------------
# content layer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    {"email": "victim@example.com"},                       # jsonb
    ["victim@example.com"],                                # array
    ("victim@example.com", 1),                             # composite
    b"victim@example.com",                                 # bytea
    bytearray(b"victim@example.com"),
    memoryview(b"victim@example.com"),
    {"user": {"contacts": [{"mail": "victim@example.com"}]}},   # nested
    [{"a": ["victim@example.com"]}],
    {"victim@example.com": 1},                             # PII in the KEY
    {"a": "victim@example.com", "b": "other@example.com"},
])
def test_no_container_shape_leaks(value):
    found = set()
    out = pii.mask_value(value, found)
    assert not _leaks(out), out
    assert found, "something should have fired"


@pytest.mark.parametrize("leaf", [
    "victim@example.com",
    "TR33 0006 1005 1978 6457 8413 26",
    "4111 1111 1111 1111",
    "10000000146",
])
def test_a_leaf_is_treated_exactly_as_the_same_string_at_top_level(leaf):
    """The invariant, stated without naming any detector: whatever the active
    detector set does to a string, it does to that string inside a container.
    Region matters here -- the Turkey detectors (tckn / vkn) are off under the
    generic default -- so asserting a specific mask would only pin the test
    config. Asserting AGREEMENT pins the behaviour.
    """
    top_found, deep_found = set(), set()
    top = pii.mask_value(leaf, top_found)
    deep = pii.mask_value({"k": [leaf]}, deep_found)
    assert deep["k"][0] == top
    assert deep_found == top_found


@pytest.mark.parametrize("value,expect_type", [
    ({"a": "victim@example.com"}, dict),
    (["victim@example.com"], list),
    (("victim@example.com",), tuple),
    ({"victim@example.com"}, set),
    (frozenset({"victim@example.com"}), frozenset),
])
def test_the_container_type_survives(value, expect_type):
    """The CSV writer, the XLSX writer and the web JSON response all receive
    this value. Turning a dict into a string would change every one of them."""
    out = pii.mask_value(value, set())
    assert type(out) is expect_type


def test_a_dict_keeps_its_keys_and_its_length():
    out = pii.mask_value({"primary": "victim@example.com", "alt": "x@y.com"},
                         set())
    assert sorted(out) == ["alt", "primary"]
    assert len(out) == 2


def test_binary_that_is_not_text_is_left_exactly_alone():
    """Decoding is what keeps the cost down: a genuine binary blob is rejected
    by the UTF-8 decoder rather than scanned."""
    raw = b"\x00\xff\xfe\x01"
    found = set()
    assert pii.mask_value(raw, found) == raw
    assert found == set()


def test_clean_bytes_stay_bytes():
    """Only a bytes value that actually needed masking changes shape."""
    raw = b"nothing to see here"
    assert pii.mask_value(raw, set()) == raw


def test_numbers_and_booleans_still_pass_through():
    """A numeric column cannot hold an '@', and the phone detector needs
    separators a numeric type does not keep — scanning them would only invent
    false positives on long integer IDs."""
    for v in (0, 1, -5, 12345678901, 3.14, True, False):
        found = set()
        assert pii.mask_value(v, found) == v
        assert found == set()


def test_deep_nesting_neither_raises_nor_leaks():
    """A recursive masker that hit Python's stack limit would raise in the
    middle of streaming a result the user is already waiting for. At the cap the
    container is masked as text: the shape changes, the masking does not."""
    deep = cur = {}
    for _ in range(60):
        cur["n"] = {}
        cur = cur["n"]
    cur["mail"] = "victim@example.com"
    found = set()
    out = pii.mask_value(deep, found)
    assert not _leaks(out)
    assert "email" in found


def test_an_empty_container_is_returned_as_itself():
    assert pii.mask_value({}, set()) == {}
    assert pii.mask_value([], set()) == []
    assert pii.mask_value((), set()) == ()


# ---------------------------------------------------------------------------
# column-name layer
# ---------------------------------------------------------------------------

def test_a_flagged_container_column_masks_every_leaf():
    found = set()
    out = pii._mask_column_value(
        {"primary": "victim@example.com", "alt": "other@example.com"},
        "email", found)
    assert not _leaks(out)
    assert out["primary"].endswith("@example.com")     # masked, still an email
    assert "email" in found


def test_the_column_masker_is_not_applied_to_dict_keys():
    """The bug this line exists for. A key is structure -- "primary", "alt",
    "street" -- and the operator's assertion is about the column's VALUES.
    Applying the email masker to the keys masked "primary" and "alt" to the
    same string; they collided, the whole mapping fell back to masking its
    repr, and the second address came out IN CLEAR:

        {'primary': 'a@b.com', 'alt': 'c@d.com'}
          -> "{***@b.com', 'alt': 'c@d.com'}"

    It also destroyed the shape: {'street': ...} became
    {'[REDACTED]': '[REDACTED]'}.
    """
    out = pii._mask_column_value(
        {"primary": "victim@example.com", "alt": "other@example.com"},
        "email", set())
    assert sorted(out) == ["alt", "primary"]
    assert "other@example.com" not in str(out)

    out = pii._mask_column_value({"street": "1 Main St", "city": "X"},
                                 "address", set())
    assert sorted(out) == ["city", "street"]
    assert out["street"] == "[REDACTED]"


def test_a_pii_key_in_a_flagged_column_is_still_masked_by_content():
    """Keys skip the column masker, so they must not skip masking altogether —
    the column layer replaces the content scan for its columns."""
    out = pii._mask_column_value({"victim@example.com": "other@example.com"},
                                 "email", set())
    assert not _leaks(out)


@pytest.mark.parametrize("value,pii_type", [
    (["Ada Lovelace", "Alan Turing"], "name"),
    ({"c": {"work": "victim@example.com"}}, "email"),
    (("victim@example.com", 5), "email"),
    (b"victim@example.com", "email"),
    ({"street": "1 Main St"}, "address"),
])
def test_no_flagged_container_shape_leaks(value, pii_type):
    assert not _leaks(pii._mask_column_value(value, pii_type, set()))


def test_binary_in_a_flagged_column_becomes_masked_hex_not_a_pass_through():
    """The column layer masks by ASSERTION, so unlike the content scan it must
    not hand back an unmasked blob just because it is not text."""
    out = pii._mask_column_value(b"\x00\xff", "email", set())
    assert out != b"\x00\xff"


def test_a_flagged_container_column_keeps_its_type():
    assert type(pii._mask_column_value({"a": "x@y.com"}, "email", set())) is dict
    assert type(pii._mask_column_value(["x@y.com"], "email", set())) is list
    assert type(pii._mask_column_value(("x@y.com",), "email", set())) is tuple


def test_deep_nesting_in_a_flagged_column_neither_raises_nor_leaks():
    deep = cur = {}
    for _ in range(60):
        cur["n"] = {}
        cur = cur["n"]
    cur["mail"] = "victim@example.com"
    assert not _leaks(pii._mask_column_value(deep, "email", set()))


# ---------------------------------------------------------------------------
# the row, and the exemption that must still win
# ---------------------------------------------------------------------------

def test_mask_row_covers_containers_alongside_scalars():
    row = ("victim@example.com",
           {"email": "victim@example.com", "iban": "TR330006100519786457841326"},
           ["victim@example.com"],
           b"victim@example.com",
           42)
    found = set()
    out = pii.mask_row(row, found)
    assert not _leaks(out)
    assert "TR330006100519786457841326" not in str(out)
    assert out[4] == 42
    assert {"email", "iban"} <= found


def test_an_exempted_column_is_still_untouched():
    """pii_masking_exemptions exist for targets holding public data. Recursing
    into containers must not quietly override that."""
    row = ({"email": "victim@example.com"},)
    out = pii.mask_row(row, set(), skip_cols={0})
    assert out[0] == {"email": "victim@example.com"}
