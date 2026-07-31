"""How a database value becomes characters in the result file.

One rule, and it is about data fidelity rather than looks: **the result must not
claim more precision than the column has.**

Python's `datetime` holds microseconds — six digits — and `str()` prints all six
whenever any of them is non-zero. A column declared with fewer sub-second digits
therefore arrives padded. SQL Server's `DATETIME` keeps milliseconds, so
`SELECT getdate()` reached the grid as `2026-07-30 14:54:36.520000` where SSMS,
and the type's own definition, say `…36.520`. Reported from the editor
2026-07-30; the three trailing zeros are an artifact of the transport, not
something the database returned.

Postgres has the same defect on any narrowed type — `now()::timestamp(3)` renders
`.721000` — so this is not a SQL Server special case. It only shows up there
first because `getdate()` is the query everyone types.

Engine-modular by construction: the precision comes from the driver's own
`cursor.description`, which every DBAPI driver fills in, so a new engine needs no
code here — see `sub_second_digits` for the one place the drivers disagree.
"""
from __future__ import annotations

import logging
from datetime import datetime, time

log = logging.getLogger(__name__)

# What Python itself can represent and print. A column declaring this many
# digits or more is already rendered as faithfully as the transport allows:
# `datetime2(7)` holds 100ns ticks that a Python datetime cannot carry, and the
# driver has already dropped the seventh digit by the time we see the value.
# Padding it back to seven would assert a digit nobody measured.
PY_SUBSECOND_DIGITS = 6


def sub_second_digits(desc_entry) -> int | None:
    """Declared sub-second digits for one `cursor.description` entry, or None.

    DBAPI leaves this to the driver and the two we run disagree — measured on
    the live fleet 2026-07-30, not assumed:

    | driver             | field              | examples                          |
    |--------------------|--------------------|-----------------------------------|
    | pyodbc / SQL Server| `description[5]`   | `datetime` 3, `datetime2(7)` 7,   |
    |                    | (scale)            | `datetime2(0)`/`smalldatetime` 0  |
    | psycopg / Postgres | `.precision`       | `timestamp(3)` 3, `timestamp(0)` 0|
    |                    | (scale is None)    | bare `timestamp`/`timestamptz` None|

    So: scale first, then precision. None means "the driver did not say", which
    for Postgres is the type's default of microseconds — exactly what Python
    already prints, so nothing needs doing.

    Only ever consult this for a value that IS temporal. `numeric(10,2)` comes
    back from psycopg as precision=10 / scale=2, and reading either as a
    sub-second count would be nonsense; the isinstance check in `row_formatter`
    is what keeps the two apart.
    """
    scale = None
    try:
        scale = desc_entry[5] if len(desc_entry) > 5 else None
    except Exception:
        scale = None
    if isinstance(scale, int) and not isinstance(scale, bool):
        return scale
    prec = getattr(desc_entry, "precision", None)
    if isinstance(prec, int) and not isinstance(prec, bool):
        return prec
    return None


def format_temporal(value, digits: int | None) -> str:
    """`str(value)` with padding zeros beyond `digits` removed.

    Never rounds, and never drops a digit that carries information: if the
    digits about to be cut are not all zero then the driver knows something the
    declared scale does not, and the value is returned in full. Trimming padding
    is presentation; dropping data would be a lie, and this file is what someone
    pastes into a ticket or hands to an auditor.
    """
    if digits is None or digits >= PY_SUBSECOND_DIGITS:
        return str(value)
    us = getattr(value, "microsecond", 0) or 0
    if not us:
        return str(value)                 # Python already prints no fraction
    if us % (10 ** (PY_SUBSECOND_DIGITS - digits)):
        return str(value)                 # trimming here would lose a real digit
    # `str()` renders exactly six digits when microsecond is non-zero, so this
    # substring is present. A targeted replace rather than string slicing, so a
    # trailing UTC offset (`…36.520000+03:00`) survives intact.
    printed = f".{us:06d}"
    trimmed = "" if digits <= 0 else "." + f"{us:06d}"[:digits]
    return str(value).replace(printed, trimmed, 1)


def row_formatter(description, skip_types: tuple = ()):
    """A `row -> row` callable for this result, or None when none is needed.

    `skip_types` names value types the CONSUMER renders better than a string
    can. The XLSX writer passes `datetime`, because openpyxl stores one as a
    real date cell that Excel can sort, filter and re-format — turning it into
    a trimmed string to remove three padding zeros would trade a cosmetic gain
    for a functional loss. Its `time` values have no native mapping and do fall
    through to `str()`, so those still get trimmed.

    Returns None — rather than a list of identity functions — when no column in
    the result declares reduced precision, so the streaming row loop keeps its
    original shape for the overwhelmingly common case and pays nothing.

    Never raises: a formatting nicety must not be the reason a delivered result
    fails. On any surprise from a driver the rows go out exactly as before.
    """
    try:
        digits = [sub_second_digits(d) for d in (description or ())]
    except Exception:
        log.debug("sub-second precision probe failed", exc_info=True)
        return None
    narrowed = [i for i, d in enumerate(digits)
                if d is not None and d < PY_SUBSECOND_DIGITS]
    if not narrowed:
        return None

    def fmt(row):
        try:
            out = list(row)
        except Exception:
            return row
        for i in narrowed:
            if i >= len(out):
                continue
            v = out[i]
            # `date` is excluded on purpose: it has no sub-second part, and
            # `datetime` (which does) is already caught by the first arm.
            if skip_types and isinstance(v, skip_types):
                continue
            if isinstance(v, (datetime, time)):
                out[i] = format_temporal(v, digits[i])
        return out

    return fmt
