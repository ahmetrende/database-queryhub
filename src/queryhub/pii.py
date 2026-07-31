"""Content-based PII masking for query result output.

The bot masks sensitive values *in the result file* (CSV / XLSX) right
before they're written, after the query has run normally against the
target. Detection is content-based: each cell value is matched against
a registry of detectors (regex + optional validator), not by column
name — so an aliased or wrapped column (`SELECT email AS e`,
`SELECT lower(email)`) can't slip PII past the mask.

Which detectors run is a REGION PACK, selected by bot_config.pii_region.
The default pack is `generic` and is country-neutral: email, payment card
(Luhn + network prefix), IBAN (ISO 13616 mod-97) and E.164 phone. National
identifiers live in their own packs — they need a country's checksum
algorithm, and a value that passes one country's check is a false positive
everywhere else — so a region is opt-in (`pii_region = tr`, or a
comma-separated list). Adding a region is one entry per detector: regex +
validator + masker, no call-site changes.

Content detection is one of two layers. The other is a column-name catalog
(`pii_column_patterns`) for free-text PII with no detectable shape — a name,
an address — which content matching cannot find by construction. Neither
layer is a data boundary; see docs/COMPLIANCE.md for where the limits are.

Toggle: bot_config.pii_masking_enabled (default 'on'). Runtime-effective.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

from . import config as cfg

log = logging.getLogger(__name__)


def is_enabled() -> bool:
    return (cfg.get_setting("pii_masking_enabled", "on") or "").strip().lower() in {
        "on", "true", "yes", "1",
    }


@dataclass(frozen=True)
class Detector:
    """A single PII kind.

    name      : short label used in the audit log + user hint (e.g. 'email').
    pattern   : compiled regex; every match in a cell is a candidate.
    validator : optional fn(match_text) -> bool. Return False to reject a
                match (e.g. checksum failure) so it is left unmasked.
                None means "any regex match is PII".
    masker    : fn(match_text) -> str, the masked replacement.
    """
    name: str
    pattern: re.Pattern
    masker: Callable[[str], str]
    validator: Callable[[str], bool] | None = None


# --- maskers ---------------------------------------------------------------


def _mask_email(s: str) -> str:
    """j***@example.com — keep first char of local part + full domain so
    the value stays recognizable/joinable-by-domain without exposing the
    mailbox. A value with no '@' (column-layer hit on a non-email cell)
    is redacted rather than mangled."""
    if "@" not in s:
        return _redact(s)
    local, _, domain = s.partition("@")
    if not local:
        return "***@" + domain
    head = local[0]
    return f"{head}***@{domain}"


def _mask_phone(s: str) -> str:
    """Keep any leading +90 / 0 prefix and the last 2 digits; star the
    rest. Non-digit separators in the original are dropped for a uniform
    masked form."""
    digits = re.sub(r"\D", "", s)
    if len(digits) < 4:
        return "*" * len(digits)
    tail = digits[-2:]
    return "*" * (len(digits) - 2) + tail


def _mask_keep(s: str, head: int, tail: int) -> str:
    """Mask the middle of `s`, keeping `head` leading and `tail` trailing
    characters. NEVER returns more characters than it was given.

    A value shorter than head+tail used to make the two slices overlap and
    produce output LONGER than the input — `_mask_card('453212')` returned
    '45323212', an 8-character "mask" of a 6-character value. That corrupted
    legitimate short columns (e.g. `card_last4`) and revealed more digits than
    it hid. Short values now reveal only their length."""
    n = len(s)
    if n <= head + tail:
        return "*" * n
    return s[:head] + "*" * (n - head - tail) + s[n - tail:]


def _mask_tckn(s: str) -> str:
    """Turkish national ID — 11 digits. Keep first 3 + last 2 so it stays
    referenceable without exposing the full identifier."""
    return _mask_keep(s, 3, 2)


def _mask_iban(s: str) -> str:
    """IBAN — keep the country code + last 4; star the middle. Strips spaces
    for a uniform masked form."""
    return _mask_keep(re.sub(r"\s", "", s), 2, 4)


def _mask_card(s: str) -> str:
    """Card PAN — keep first 4 (network) + last 4; star the middle.
    Strips separators for a uniform masked form."""
    return _mask_keep(re.sub(r"\D", "", s), 4, 4)


def _mask_vkn(s: str) -> str:
    """Turkish tax number — keep first 3 + last 2."""
    return _mask_keep(s, 3, 2)


def _mask_name(s: str) -> str:
    """Person name — keep the first letter of each whitespace token,
    star the rest. 'John Doe' -> 'J*** D***'. Empty / 1-char tokens
    pass through as a single star."""
    out = []
    for tok in s.split():
        out.append(tok[0] + "*" * (len(tok) - 1) if len(tok) > 1 else "*")
    return " ".join(out) if out else s


def _redact(s: str) -> str:
    """Free-text PII with no useful partial form (address etc.)."""
    return "[REDACTED]"


# --- validators (checksums keep false positives near zero) -----------------


def _valid_tckn(s: str) -> bool:
    """Turkish national ID checksum. 11 digits, first non-zero,
    d10 = ((d1+d3+d5+d7+d9)*7 - (d2+d4+d6+d8)) % 10,
    d11 = (d1..d10) % 10."""
    if len(s) != 11 or not s.isdigit() or s[0] == "0":
        return False
    d = [int(c) for c in s]
    odd = d[0] + d[2] + d[4] + d[6] + d[8]
    even = d[1] + d[3] + d[5] + d[7]
    if (odd * 7 - even) % 10 != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]


def _valid_vkn(s: str) -> bool:
    """Turkish tax number (VKN) checksum. 10 digits; each of the first 9
    feeds a per-position transform, the 10th is the check digit."""
    if len(s) != 10 or not s.isdigit():
        return False
    d = [int(c) for c in s]
    total = 0
    for i in range(9):
        tmp = (d[i] + (9 - i)) % 10
        if tmp == 0:
            total += 0
        else:
            p = (tmp * (2 ** (9 - i))) % 9
            total += 9 if p == 0 else p
    check = (10 - (total % 10)) % 10
    return check == d[9]


def _valid_iban_tr(s: str) -> bool:
    """TR IBAN mod-97 check. TR + 24 alphanumerics (26 chars total).
    Move the first 4 chars to the end, map letters A=10..Z=35, then the
    integer mod 97 must equal 1."""
    compact = re.sub(r"\s", "", s).upper()
    if not re.fullmatch(r"TR\d{24}", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


# ISO 13616 IBAN length per country. An IBAN's length is fixed per country, so
# checking it before mod-97 keeps the generic detector from firing on arbitrary
# alphanumeric blobs that merely start with two letters and two digits.
_IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16,
    "BG": 22, "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28,
    "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24,
    "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18,
    "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23,
    "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32,
    "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24,
    "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15,
    "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22,
    "SA": 24, "SC": 31, "SD": 18, "SE": 24, "SI": 19, "SK": 24, "SM": 27,
    "ST": 25, "SV": 28, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22,
    "VG": 24, "XK": 20,
}


def _valid_iban(s: str) -> bool:
    """Country-agnostic IBAN check: known country code, the exact length that
    country uses, then the ISO 7064 mod-97 checksum. The mod-97 arithmetic is
    the same one the TR validator used — only the country/length gate is new,
    which is what makes the detector work outside Turkey."""
    compact = re.sub(r"[\s\-]", "", s).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", compact):
        return False
    expected = _IBAN_LENGTHS.get(compact[:2])
    if expected is None or len(compact) != expected:
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum: double every second digit from the right, subtract 9
    if >9, sum, mod 10 must be 0."""
    total = 0
    for i, c in enumerate(reversed(digits)):
        n = int(c)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# Major card network prefixes — enough to reject Luhn-passing random
# numbers without a full BIN database.
_CARD_PREFIX_RE = re.compile(
    r"^(?:"
    r"4"                      # Visa
    r"|5[1-5]"               # Mastercard
    r"|2(?:2[2-9]|[3-6]\d|7[01]|720)"  # Mastercard 2-series (2221-2720)
    r"|3[47]"               # Amex
    r"|9792"                # Troy
    r")"
)


def _valid_card(s: str) -> bool:
    """13-19 digit PAN that passes Luhn AND starts with a known network
    prefix. The prefix check kills false positives on random Luhn-valid
    numbers (e.g. some internal IDs)."""
    digits = re.sub(r"\D", "", s)
    if not (13 <= len(digits) <= 19):
        return False
    if not _CARD_PREFIX_RE.match(digits):
        return False
    return _luhn_ok(digits)


# --- patterns --------------------------------------------------------------

# Email: standard-enough address. Anchored loosely so it matches inside a
# larger cell value too.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)

# Turkish mobile phone — CONSERVATIVE on purpose to avoid masking plain
# numeric IDs / amounts. Requires an explicit phone marker:
#   +90 5xx ... / 0090 5xx ... / 0 5xx ... / or a separated 5xx group.
# A bare 10-digit run with no prefix and no separators is NOT matched.
_PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:"
    r"(?:\+?90[\s.\-]?|0)?5\d{2}[\s.\-]\d{3}[\s.\-]?\d{2}[\s.\-]?\d{2}"  # separated
    r"|"
    r"(?:\+90|0090|0)5\d{9}"                                            # prefixed, no sep
    r")"
    r"(?!\d)"
)

# IBAN (TR pack): TR + 24 digits, optionally space-grouped. Validator does mod-97.
_IBAN_RE = re.compile(r"\bTR\d{2}(?:[\s]?\d){22}\b", re.IGNORECASE)

# IBAN (generic pack): any ISO 13616 country code + check digits + BBAN, with
# optional space/hyphen grouping. The validator enforces the country's exact
# length and the mod-97 checksum, so this wide pattern does not over-match.
_IBAN_ANY_RE = re.compile(
    r"\b[A-Z]{2}\d{2}(?:[\s\-]?[A-Z0-9]){10,30}\b", re.IGNORECASE)

# Phone (generic pack): E.164 only — an explicit '+' and 8-15 digits. Requiring
# the '+' is deliberate: a bare 10-digit run is indistinguishable from an order
# id or an amount, and masking those corrupts data (the same trap the TR-only
# 10-digit tax-number detector fell into outside Turkey).
_PHONE_E164_RE = re.compile(
    # \d{0,3} (not {1,3}) so single-digit country codes like "+1 415 555 0123"
    # match — the country code can be followed immediately by a separator.
    r"(?<![\d+])\+[1-9]\d{0,3}[\s.\-]?(?:\d[\s.\-]?){6,12}\d(?!\d)")

# Card PAN: 13-19 digits, optionally separated by spaces/hyphens in
# 4-digit groups. Validator does Luhn + network prefix.
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?<=\d)")

# Turkish national ID: exactly 11 digits, not part of a longer run.
# Validator does the TC checksum.
_TCKN_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")

# Turkish tax number: exactly 10 digits, not part of a longer run.
# Validator does the VKN checksum.
_VKN_RE = re.compile(r"(?<!\d)\d{10}(?!\d)")


# Order matters within a pack: each detector masks in turn over the
# (progressively masked) text, so the longest / most specific kinds run first.
# IBAN and card (13-19 digits) before the shorter numeric kinds; email is
# independent (it needs '@').
#
# Detectors come in REGION PACKS, chosen by bot_config `pii_region`.
#
# `generic` is the default and is country-neutral: email, Luhn-checked card,
# IBAN for every ISO 13616 country, and E.164 phone numbers. This is the right
# default for any deployment, and critically it does NOT include the two
# detectors that key on bare digit runs.
#
# `tr` adds the Turkey-specific kinds. Those two — an 11-digit national id and
# a 10-digit tax number — match ANY numeric string of that length and are
# separated from ordinary data only by a national checksum. Roughly a tenth of
# arbitrary 10-digit values satisfy the tax-number checksum, so outside Turkey
# the pack both mangles unrelated columns (an unformatted US phone number comes
# back as a "masked tax number") and gives false assurance in the audit trail.
# Keeping them in an opt-in pack is what makes the default safe abroad.
_PACK_GENERIC: list[Detector] = [
    Detector(name="email", pattern=_EMAIL_RE, masker=_mask_email),
    Detector(name="iban",  pattern=_IBAN_ANY_RE, masker=_mask_iban, validator=_valid_iban),
    Detector(name="card",  pattern=_CARD_RE, masker=_mask_card, validator=_valid_card),
    Detector(name="phone", pattern=_PHONE_E164_RE, masker=_mask_phone),
]

_PACK_TR: list[Detector] = [
    Detector(name="email", pattern=_EMAIL_RE, masker=_mask_email),
    Detector(name="iban",  pattern=_IBAN_RE, masker=_mask_iban, validator=_valid_iban_tr),
    Detector(name="card",  pattern=_CARD_RE, masker=_mask_card, validator=_valid_card),
    Detector(name="tckn",  pattern=_TCKN_RE, masker=_mask_tckn, validator=_valid_tckn),
    Detector(name="vkn",   pattern=_VKN_RE, masker=_mask_vkn, validator=_valid_vkn),
    Detector(name="phone", pattern=_PHONE_RE, masker=_mask_phone),
]

REGION_PACKS: dict[str, list[Detector]] = {
    "generic": _PACK_GENERIC,
    "tr": _PACK_TR,
}

# Back-compat alias: the module used to expose a single flat DETECTORS list.
DETECTORS: list[Detector] = _PACK_GENERIC


def region() -> str:
    """Configured detector pack name (bot_config `pii_region`, default
    `generic`). An unknown value falls back to `generic` rather than silently
    masking nothing."""
    from . import config as cfg
    try:
        val = (cfg.get_setting("pii_region", "generic") or "").strip().lower()
    except Exception:
        return "generic"
    return val if val in REGION_PACKS else "generic"


def active_detectors() -> list[Detector]:
    """The detector pack in force for this deployment."""
    return REGION_PACKS[region()]


# --- core API --------------------------------------------------------------


# A cell can be a whole document, not just a scalar. psycopg hands jsonb back
# as a dict, an array as a list, a composite as a tuple and bytea as bytes, and
# every one of those used to walk straight through the masker: it opened with
# `if not isinstance(value, str): return value`. Measured on this deployment
# 2026-07-30, all four reach the delivered CSV in clear:
#
#     bytea      b'victim@example.com'
#     jsonb      {'email': 'victim@example.com'}
#     array      ['victim@example.com']
#     composite  ('victim@example.com', '1')
#
# A jsonb blob is the worst of them, because one column can hold every PII kind
# at once, and `SELECT payload FROM events` is an ordinary query. So containers
# are walked to their leaves, and the CONTAINER TYPE IS PRESERVED — the CSV and
# XLSX writers, the Slack preview and the web JSON response all keep receiving
# the shape they got before.
#
# Depth is capped. Nothing psycopg produces is cyclic, but nesting is
# unbounded, and a recursive masker that hits Python's stack limit would raise
# in the middle of streaming a result the user is already waiting for. At the
# cap the container is serialised and masked AS TEXT instead: still no leak,
# and the failure mode is a changed shape rather than a lost result.
_MAX_MASK_DEPTH = 12


def mask_value(value, found: set[str], _depth: int = 0) -> object:
    """Mask any PII inside a single cell value. Returns the (possibly
    rewritten) value; records detector names that fired into `found`.

    Strings are scanned by every active detector. Containers (dict / list /
    tuple / set) are walked to their leaves and rebuilt with the same type.
    bytes-like values are scanned as UTF-8 text when they decode as such.
    Numbers, booleans and None pass through: a numeric column cannot hold an
    '@', and the phone detector needs separators a numeric type does not keep,
    so scanning them would only add false positives on long integer IDs.
    """
    if value is None:
        return None

    if isinstance(value, str):
        out = value
        for det in active_detectors():
            def _repl(m: re.Match, _det=det) -> str:
                text = m.group(0)
                if _det.validator is not None and not _det.validator(text):
                    return text  # validation failed → leave as-is
                found.add(_det.name)
                return _det.masker(text)
            out = det.pattern.sub(_repl, out)
        return out

    # bool before int: bool IS an int in Python, and True/False carry nothing.
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value

    if isinstance(value, (bytes, bytearray, memoryview)):
        return _mask_bytes(value, found)

    if isinstance(value, (dict, list, tuple, set, frozenset)):
        if _depth >= _MAX_MASK_DEPTH:
            # Too deep to walk safely: mask the serialisation. Losing the shape
            # is acceptable; losing the masking is not.
            return mask_value(str(value), found)
        return _mask_container(value, found, _depth)

    return value


def _mask_bytes(value, found: set[str]):
    """Scan a bytes-like cell as UTF-8 text.

    A bytea column holding UTF-8 text renders in the result file as
    `b'victim@example.com'` — fully readable — so it is a real leak channel and
    not a theoretical one. Binary that is not UTF-8 fails to decode and is left
    exactly as it was, which is also what keeps the cost down: a genuine binary
    blob is rejected by the decoder rather than scanned.

    When something fires, the masked TEXT is returned rather than re-encoded
    bytes. Re-encoding would claim the stored bytes are something they are not,
    and every consumer of this value renders it as text anyway.
    """
    raw = bytes(value)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return value
    before = len(found)
    masked = mask_value(text, found)
    if len(found) == before and masked == text:
        return value                     # nothing to mask: keep the bytes
    return masked


def _mask_container(value, found: set[str], _depth: int):
    """Walk a dict / list / tuple / set, masking leaves, keeping the type.

    Dictionary KEYS are masked too — an email-keyed map is a real shape, and a
    key is as readable as a value. Masking keys can make two of them collide
    (two addresses that mask to the same string), which would silently merge
    entries and lose a row's worth of data from something an auditor may rely
    on. When that happens the whole mapping is masked as TEXT instead: the
    content survives in full, masked, and only the shape changes.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            mk = mask_value(k, found, _depth + 1) if isinstance(k, str) else k
            out[mk] = mask_value(v, found, _depth + 1)
        if len(out) != len(value):
            return mask_value(str(value), found)
        return out
    masked = [mask_value(v, found, _depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(masked)
    if isinstance(value, (set, frozenset)):
        return type(value)(masked)
    return masked


def mask_row(row: tuple | list, found: set[str],
             col_map: dict | None = None,
             skip_cols: set[int] | None = None) -> list:
    """Mask every cell in a result row. `found` accumulates which PII
    kinds fired across the whole result (for the audit log + user hint).

    Two layers:
      - col_map (column-name catalog): {col_index: pii_type}. A column
        whose NAME matched a pii_column_patterns row is masked by type —
        this is the only way to catch free-text PII (name / address).
      - content scan: every other cell is value-scanned by DETECTORS.
    The column layer wins for its columns (more specific signal).

    `skip_cols` are column indexes covered by a pii_masking_exemptions
    row — those cells pass through untouched (no catalog, no content
    scan)."""
    out = []
    for i, v in enumerate(row):
        if skip_cols and i in skip_cols:
            out.append(v)
        elif col_map and i in col_map:
            out.append(_mask_column_value(v, col_map[i], found))
        else:
            out.append(mask_value(v, found))
    return out


# --- column-name catalog layer ---------------------------------------------

# When a COLUMN is flagged PII by name, the operator has asserted the
# column holds that kind — so we mask the value directly by type,
# WITHOUT re-checking the content format. This is the key difference
# from the content layer: a phone column holding a bare 10-digit number
# (no +90 / separators) still gets masked, because the column name —
# not the value shape — is the signal. Each type maps to a value masker
# that works on any string.
_COLUMN_MASKERS = {
    "email":   _mask_email,
    "phone":   _mask_phone,
    "tckn":    _mask_tckn,
    "vkn":     _mask_vkn,
    "iban":    _mask_iban,
    "card":    _mask_card,
    "name":    _mask_name,
    "address": _redact,
    "generic": _redact,
}


def _mask_column_value(value, pii_type: str, found: set[str]):
    """Mask a value in a column the catalog flagged as `pii_type`. The
    column name is the authority — the value is masked by type without a
    content-format re-check, so a phone column with a bare digit run is
    still masked."""
    if value is None:
        return None
    masker = _COLUMN_MASKERS.get(pii_type, _redact)

    # A container in a flagged column: apply the column's masker to each LEAF
    # rather than to the container's repr. `_mask_email(str({'email': ...}))`
    # produced one mangled blob that was neither readable nor reliably masked —
    # the email masker looks for an '@' in a string that also holds braces,
    # quotes and every other key. Walking to the leaves keeps the shape and
    # applies the operator's assertion about the column to each value in it.
    if isinstance(value, (dict, list, tuple, set, frozenset, bytes, bytearray,
                          memoryview)):
        return _mask_column_container(value, pii_type, masker, found)

    s = value if isinstance(value, str) else str(value)
    found.add(pii_type)
    return masker(s)


def _mask_column_container(value, pii_type: str, masker, found: set[str],
                           _depth: int = 0):
    """`_mask_column_value` for a container cell: same type out, masker applied
    to every leaf. Depth-capped for the same reason as `mask_value`."""
    if _depth >= _MAX_MASK_DEPTH:
        found.add(pii_type)
        return masker(str(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            text = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            found.add(pii_type)
            return masker(bytes(value).hex())
        found.add(pii_type)
        return masker(text)
    if isinstance(value, dict):
        # KEYS get the ordinary content scan, not the column's masker. A key is
        # structure — "primary", "alt", "street" — and the operator's assertion
        # is about the column's VALUES. Applying an email masker to the keys of
        # a jsonb email column masked "primary" and "alt" to the same string,
        # they collided, the whole mapping fell back to masking its repr, and
        # the second address came out IN CLEAR. Measured before this line
        # existed: {'primary': 'a@b.com', 'alt': 'c@d.com'} delivered as
        # "{***@b.com', 'alt': 'c@d.com'}". It also destroyed the shape:
        # {'street': ...} became {'[REDACTED]': '[REDACTED]'}.
        return {
            (mask_value(k, found) if isinstance(k, str) else k):
                _mask_column_container(v, pii_type, masker, found, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        masked = [_mask_column_container(v, pii_type, masker, found, _depth + 1)
                  for v in value]
        if isinstance(value, tuple):
            return tuple(masked)
        if isinstance(value, (set, frozenset)):
            return type(value)(masked)
        return masked
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (int, float)):
        # A number inside a flagged container is still that column's kind, so
        # it is masked — but as text, since a masked number is not a number.
        found.add(pii_type)
        return masker(str(value))
    found.add(pii_type)
    return masker(value if isinstance(value, str) else str(value))


def _load_column_patterns() -> list[tuple]:
    """Read the enabled column-name patterns for the CONFIGURED REGION. Returns
    (pattern, pii_type, match_type, exclude_tokens) tuples. Called once per
    result (in column_pii_map), not per row.

    A pattern with `region IS NULL` applies everywhere; one naming a region only
    fires when `bot_config pii_region` matches it — the same switch the value
    detectors already use, so the two halves of the catalog agree about where
    they are. This exists because the Turkish tokens are ordinary words
    elsewhere: on a fresh install outside Turkey `ad_id`, `cep` and `pan` were
    all masked, and `product_name` came back as `W***** M****`.

    A missing `region` column (an install that has not run migration 088 yet)
    falls back to loading everything, which is the pre-migration behaviour —
    never less masking than before.
    """
    from . import db
    try:
        rows = db.fetch_all(
            "SELECT pattern, pii_type, match_type, exclude_tokens "
            "  FROM pii_column_patterns "
            " WHERE enabled = TRUE "
            "   AND (region IS NULL OR lower(region) = %s)",
            (region(),),
        )
    except Exception:
        log.warning("pii_column_patterns has no region/exclude_tokens column "
                    "yet (migration 088 not applied?) — loading every pattern")
        rows = db.fetch_all(
            "SELECT pattern, pii_type, match_type FROM pii_column_patterns "
            "WHERE enabled = TRUE"
        )
    return [(r["pattern"].lower(), r["pii_type"], r["match_type"],
             tuple(x.lower() for x in (r.get("exclude_tokens") or ())))
            for r in rows]


# --- masking exemptions (public-data scopes) --------------------------------
#
# Some targets hold public data (e.g. an OpenSanctions mirror) where masking
# is technically correct but business-wise wrong. pii_masking_exemptions
# scopes opt-outs at target / database / table / column granularity (NULL =
# wildcard). Resolved once per statement, before streaming.


def _load_exemptions(target_id: int, database: str) -> list[dict]:
    """Enabled exemption rows matching (target, database-or-wildcard)."""
    from . import db
    return db.fetch_all(
        "SELECT database_name, table_name, column_name, apply_in_joins, "
        "       keep_value_scan "
        "  FROM pii_masking_exemptions "
        " WHERE enabled AND target_server_id = %s "
        "   AND (database_name IS NULL OR database_name = %s)",
        (target_id, database),
    )


def _tables_in(sql: str, engine: str = "postgres") -> set[str] | None:
    """Lowercased table names a statement references, minus CTE aliases
    (a CTE name shows up as an exp.Table when selected from). Returns
    None when the SQL can't be parsed — callers must treat that as
    'unknown' and keep masking ON (fail-closed)."""
    try:
        import sqlglot
        from sqlglot import exp
        from . import engines
        dialect = engines.spec(engine).sqlglot_dialect
        tables: set[str] = set()
        ctes: set[str] = set()
        for s in sqlglot.parse(sql, read=dialect):
            if s is None:
                continue
            for cte in s.find_all(exp.CTE):
                ctes.add(cte.alias_or_name.lower())
            for t in s.find_all(exp.Table):
                tables.add(t.name.lower())
        tables -= ctes
        return tables or None
    except Exception:
        return None


def _column_skips(col_rows: list[dict], tables: set[str] | None,
                  columns: list[str]) -> set[int]:
    """Pure: result-column indexes exempted by column-level rows.

    `tables` is the set of table names in the query (None = unparseable).
    Per row:
      - column-scoped (table_name NULL): matches the column name anywhere.
      - table-scoped, strict (apply_in_joins FALSE): only when the query
        reads SOLELY that table (provenance certain); fail-closed otherwise.
      - table-scoped, apply_in_joins TRUE: fires whenever its table is among
        the query's tables — accepts a same-named co-joined column also being
        unmasked (opt-in, for db-wide-non-sensitive names)."""
    skip: set[int] = set()
    for i, col in enumerate(columns):
        name = (col or "").lower()
        for r in col_rows:
            if name != r["column_name"].lower():
                continue
            if not r["table_name"]:
                skip.add(i)
                break
            if not tables:
                continue  # table-scoped + unparseable SQL -> fail-closed
            tbl = r["table_name"].lower()
            matched = (tbl in tables) if r.get("apply_in_joins") else (tables == {tbl})
            if matched:
                skip.add(i)
                break
    return skip


def exemption_decision(target_id: int, database: str, sql: str,
                       columns: list[str],
                       engine: str = "postgres") -> tuple[bool, set[int]]:
    """Resolve pii_masking_exemptions for one statement's result.

    Returns (skip_all, skip_cols):
      skip_all=True  -> masking fully off for this result. Granted by a
                        target/db-wide row, or by table-level rows when the
                        query references ONLY exempt tables. A join that
                        touches any non-exempt table keeps masking ON, and
                        unparseable SQL never lifts masking (fail-closed —
                        a column from a PII table must not slip through by
                        riding along an exempt table's query).
      skip_cols      -> result-column indexes exempted by column-level rows
                        (matched by name; a table-scoped column row also
                        requires the only-exempt-tables condition)."""
    rows = _load_exemptions(target_id, database)
    if not rows:
        return False, set()

    # Target- or database-wide exemption: no table/column narrowing.
    if any(r["table_name"] is None and r["column_name"] is None for r in rows):
        return True, set()

    tables = _tables_in(sql, engine=engine)
    exempt_tables = {r["table_name"].lower()
                     for r in rows
                     if r["table_name"] and r["column_name"] is None}
    only_exempt_tables = bool(tables) and tables <= exempt_tables
    if only_exempt_tables and exempt_tables:
        return True, set()

    # Full-skip column rows only. "Soft" rows (keep_value_scan) drop the
    # column-name rule but keep the content scan — resolved by
    # exemption_namescan, NOT here, so they never fully pass through.
    col_rows = [r for r in rows
                if r["column_name"] and not r.get("keep_value_scan")]
    return False, _column_skips(col_rows, tables, columns)


def exemption_namescan(target_id: int, database: str, sql: str,
                       columns: list[str],
                       engine: str = "postgres") -> set[int]:
    """Result-column indexes for SOFT exemptions (keep_value_scan=true):
    the column-name mask is lifted, but the per-value content detectors still
    run. For a column that is mostly non-PII yet can hold the odd real PII
    (e.g. a crypto address column that also stores fiat IBANs) — genuine
    values pass, IBAN/card/TCKN/email cells still get masked. Same
    table-scoping rules as full column exemptions (fail-closed on unparseable
    SQL for table-scoped rows)."""
    rows = _load_exemptions(target_id, database)
    ns_rows = [r for r in rows
               if r["column_name"] and r.get("keep_value_scan")]
    if not ns_rows:
        return set()
    tables = _tables_in(sql, engine=engine)
    return _column_skips(ns_rows, tables, columns)


def _match_pii_type(name: str, patterns) -> str | None:
    """Return the pii_type a (column) name matches in the catalog, or None.
    Shared by the result-column pass and the source-column pass.

    `token` splits on `_`, whitespace and `-`, so it is NOT narrower than
    `substring` in the way the seed migration's comment claims: it says the
    `name` token was chosen over the substring because "substring 'name' would
    catch file_name etc.", but the splitter gives {file, name} and the token
    rule matches file_name too — measured. That comment sits in an applied
    migration whose checksum the ledger records, so the correction lives here,
    where anyone choosing a match_type will read it. What actually keeps `name`
    off metadata is the exclusion list below.

    A pattern may carry `exclude_tokens`: qualifiers whose presence in the
    column name means this is not that kind of column after all. The broad
    `name` token needs it — measured across 99,479 real fleet columns, 6,989
    contain it and the top ten are `database_name` (1913), `schema_name` (773),
    `table_name` (673), `host_name`, `application_name`, `program_name`,
    `index_name` and `object_name`, against `full_name` at 56. A SQL gateway
    reads more database metadata than anything else, so without this the
    catalog's most-used rule is mostly wrong.

    Exclusion is NARROWING only: a name with no qualifier matches exactly as it
    did before, so this can never mask less than the pattern alone would on real
    PII. `full_name` has no metadata qualifier in it; `database_name` does.
    """
    name = (name or "").lower()
    tokens = set(re.split(r"[_\s\-]+", name))
    for entry in patterns:
        pat, ptype, mtype = entry[0], entry[1], entry[2]
        excludes = entry[3] if len(entry) > 3 else None
        matched = False
        if mtype == "token" and pat in tokens:
            matched = True
        elif mtype == "substring" and pat in name:
            matched = True
        elif mtype == "regex":
            try:
                matched = re.search(pat, name) is not None
            except re.error:
                continue
        if not matched:
            continue
        if excludes and any(x in tokens or x in name for x in excludes):
            continue                      # a qualifier says this is not PII
        return ptype
    return None


def _source_columns_by_position(sql: str, columns: list[str],
                                engine: str = "postgres") -> list[set[str]] | None:
    """Best-effort map of each RESULT column position to the set of SOURCE
    column names it derives from — closing the alias / cast masking bypass
    `full_name AS x`, `tckn::bigint AS y`, `lower(email) AS e` all
    rename the output column so the by-name catalog misses them, and a
    numeric cast additionally dodges the (string-only) content scan. Mapping
    the output position back to its underlying column(s) lets the catalog
    fire on the real name.

    Returns a list parallel to `columns` (each a set of lowercased source
    names, possibly empty), or None when the query can't be mapped
    positionally — `SELECT *` / `t.*` (names need the schema to expand), an
    arity mismatch, or unparseable SQL. Callers then fall back to result-name
    matching only. Augmentation only ADDS names to check, so it can never
    UNmask a value."""
    try:
        import sqlglot
        from sqlglot import exp
        from . import engines
        dialect = engines.spec(engine).sqlglot_dialect
    except Exception:
        return None
    try:
        stmts = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception:
        return None
    n = len(columns)
    for stmt in stmts:
        select = stmt if isinstance(stmt, exp.Select) else stmt.find(exp.Select)
        if select is None:
            continue
        projections = list(select.expressions)
        if len(projections) != n:
            continue
        if any(p.find(exp.Star) is not None for p in projections):
            continue
        direct = [{c.name.lower() for c in p.find_all(exp.Column) if c.name}
                  for p in projections]
        # Follow aliases INTO derived tables / CTEs. The outer projection of
        #     SELECT a FROM (SELECT full_name AS a FROM customers) t
        # references only `a`, so name matching alone saw nothing and the whole
        # catalog silently stopped applying — every name shipped in clear. Same
        # for a CTE. Resolve each name through the query's alias definitions
        # (transitively, since subqueries nest) so the physical column behind
        # the alias is checked too.
        aliases = _alias_sources(stmt)
        return [_expand_aliases(names, aliases) for names in direct]
    return None


def _alias_sources(tree) -> dict[str, set[str]]:
    """`{alias_name: {column names it is defined from}}` for every alias in the
    statement, at any nesting depth. Lowercased."""
    from sqlglot import exp

    out: dict[str, set[str]] = {}
    for alias in tree.find_all(exp.Alias):
        name = (alias.alias or "").lower()
        if not name:
            continue
        srcs = {c.name.lower() for c in alias.this.find_all(exp.Column) if c.name}
        if isinstance(alias.this, exp.Column) and alias.this.name:
            srcs.add(alias.this.name.lower())
        if srcs:
            out.setdefault(name, set()).update(srcs)
    return out


def _expand_aliases(names: set[str], aliases: dict[str, set[str]]) -> set[str]:
    """Close `names` over the alias map, so an outer reference resolves to the
    physical column it ultimately reads. Visited-set guards the cycle a
    self-referential alias (`x AS x`) would otherwise create."""
    out = set(names)
    pending = list(names)
    while pending:
        cur = pending.pop()
        for src in aliases.get(cur, ()):  # noqa: SIM118 - dict.get default
            if src not in out:
                out.add(src)
                pending.append(src)
    return out


def column_pii_map(columns: list[str], sql: str | None = None,
                   engine: str = "postgres",
                   lineage: list[set[str]] | None = None) -> dict:
    """Map result-column indexes to a pii_type using the catalog. Empty
    when masking is disabled or nothing matches. Computed once per result
    set, before streaming rows.

    Three sources of candidate names, and the map is their UNION — each one can
    only add coverage, never take it away:

    1. The output column's own name.
    2. `sql` given: the source column(s) the STATIC resolver can follow —
       through aliases, casts, derived tables and CTEs, so `full_name AS x` or
       `tckn::bigint` is still caught.
    3. `lineage` given: what the DATABASE says the columns come from, from
       `pii_lineage.source_columns`. This is the only one that sees through a
       VIEW, whose body is not in the submitted SQL — before it, a view that
       renamed a column made `name` / `address` / `birth_date` ship in clear
       (measured 2026-07-30; the value scan still covered email / phone / TCKN /
       IBAN / card, which is why it stayed hidden so long).

    Order matters only for cost: the cheapest rule runs first and a column that
    already matched is not re-checked.
    """
    if not is_enabled():
        return {}
    patterns = _load_column_patterns()
    if not patterns:
        return {}
    result: dict[int, str] = {}
    for i, col in enumerate(columns):
        pt = _match_pii_type(col, patterns)
        if pt is not None:
            result[i] = pt

    def _augment(src: list[set[str]] | None) -> None:
        if not src:
            return
        for i, names in enumerate(src):
            if i in result or i >= len(columns):
                continue
            for nm in names:
                pt = _match_pii_type(nm, patterns)
                if pt is not None:
                    result[i] = pt
                    break

    if sql:
        _augment(_source_columns_by_position(sql, columns, engine=engine))
    _augment(lineage)
    return result
