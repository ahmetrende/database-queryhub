"""Sub-second precision in the result file.

`SELECT getdate()` on SQL Server reached the grid as `2026-07-30 14:54:36.520000`.
DATETIME keeps milliseconds — three digits — so the last three zeros were never in
the database; they are Python's `datetime` printing all six of its microsecond
digits, and `csv.writer` calling `str()`. Reported from the editor 2026-07-30.

The rule these tests pin down is narrow on purpose: **strip padding, never drop
data.** A result file is pasted into tickets and handed to auditors, so a
formatter that quietly rounded would be worse than the padding it replaced.
"""
from datetime import date, datetime, time, timezone, timedelta
from decimal import Decimal

from dba_slack_bot import cell_format


class _Desc(tuple):
    """A cursor.description entry. pyodbc yields a plain 7-tuple; psycopg yields
    an object whose `.precision` / `.scale` are attributes AND which indexes like
    the tuple. This models both so the tests exercise the real disagreement."""
    def __new__(cls, name, scale=None, precision=None):
        self = super().__new__(cls, (name, None, None, None, precision, scale, None))
        self.precision = precision
        self.scale = scale
        return self


# ---------- which field the drivers put it in ----------

def test_pyodbc_reports_it_as_scale():
    """Measured on SQL Server: datetime 3, datetime2(7) 7, smalldatetime 0."""
    assert cell_format.sub_second_digits(_Desc("dt", scale=3)) == 3
    assert cell_format.sub_second_digits(_Desc("dt2", scale=7)) == 7
    assert cell_format.sub_second_digits(_Desc("sdt", scale=0)) == 0


def test_psycopg_reports_it_as_precision():
    """Measured on Postgres: timestamp(3) -> precision 3, scale None."""
    assert cell_format.sub_second_digits(_Desc("ts3", precision=3)) == 3
    assert cell_format.sub_second_digits(_Desc("ts0", precision=0)) == 0


def test_a_bare_postgres_timestamp_says_nothing():
    """None means "the driver did not say" — for Postgres that is the type's
    default of microseconds, which is what Python already prints. Guessing a
    number here would trim a value the database really does carry."""
    assert cell_format.sub_second_digits(_Desc("ts")) is None
    assert cell_format.format_temporal(datetime(2026, 7, 30, 12, 0, 0, 721343), None) \
        == "2026-07-30 12:00:00.721343"


# ---------- the trim itself ----------

def test_the_reported_case():
    got = cell_format.format_temporal(datetime(2026, 7, 30, 14, 54, 36, 520000), 3)
    assert got == "2026-07-30 14:54:36.520"


def test_zero_digits_drops_the_dot_entirely():
    assert cell_format.format_temporal(datetime(2026, 7, 30, 14, 54, 36, 0), 0) \
        == "2026-07-30 14:54:36"


def test_never_drops_a_non_zero_digit():
    """The declared scale says 3 but the driver handed back six real digits.
    Trimming would lose data, so the value goes out in full — the formatter's job
    is to remove padding, not to decide what is significant."""
    v = datetime(2026, 7, 30, 14, 54, 36, 123456)
    assert cell_format.format_temporal(v, 3) == "2026-07-30 14:54:36.123456"


def test_a_scale_python_cannot_hold_is_left_alone():
    """datetime2(7) is 100ns ticks; a Python datetime cannot carry the seventh
    digit and the driver has already dropped it. Padding it back would assert a
    digit nobody measured."""
    v = datetime(2026, 7, 30, 15, 6, 40, 693333)
    assert cell_format.format_temporal(v, 7) == "2026-07-30 15:06:40.693333"


def test_a_utc_offset_survives():
    """Substring replace rather than slicing, precisely so the offset is not
    eaten with the padding."""
    v = datetime(2026, 7, 30, 14, 54, 36, 520000,
                 tzinfo=timezone(timedelta(hours=3)))
    assert cell_format.format_temporal(v, 3) == "2026-07-30 14:54:36.520+03:00"


def test_time_values_too():
    assert cell_format.format_temporal(time(15, 6, 40, 693000), 3) == "15:06:40.693"


# ---------- the row formatter ----------

def test_no_formatter_when_nothing_needs_one():
    """Returning None keeps the streaming loop's original shape — the common
    case must not pay for this."""
    assert cell_format.row_formatter([_Desc("ts"), _Desc("n", scale=None)]) is None
    assert cell_format.row_formatter([]) is None
    assert cell_format.row_formatter(None) is None


def test_a_numeric_column_is_not_a_timestamp():
    """The trap. psycopg reports `numeric(10,2)` as precision=10 / scale=2, so a
    formatter that trusted the number without checking the value's type would
    treat 2 as a sub-second count and rewrite money."""
    desc = [_Desc("amount", precision=10, scale=2), _Desc("dt", scale=3)]
    fmt = cell_format.row_formatter(desc)
    assert fmt is not None
    out = fmt([Decimal("123.45"), datetime(2026, 7, 30, 14, 54, 36, 520000)])
    assert out[0] == Decimal("123.45")            # untouched, still a Decimal
    assert out[1] == "2026-07-30 14:54:36.520"


def test_dates_and_none_and_text_pass_through():
    desc = [_Desc("d", scale=0), _Desc("s", scale=0), _Desc("nil", scale=0)]
    fmt = cell_format.row_formatter(desc)
    out = fmt([date(2026, 7, 30), "text", None])
    assert out == [date(2026, 7, 30), "text", None]


def test_a_short_row_does_not_raise():
    """Belt and braces: the description and the row disagreeing must not be the
    reason a delivered result fails."""
    fmt = cell_format.row_formatter([_Desc("a", scale=3), _Desc("b", scale=3)])
    assert fmt([datetime(2026, 7, 30, 1, 2, 3, 400000)]) == ["2026-07-30 01:02:03.400"]


def test_a_hostile_driver_cannot_break_the_result():
    class Boom:
        def __len__(self): raise RuntimeError("nope")
    assert cell_format.row_formatter([Boom()]) is None


# ---------- the wiring ----------

def test_the_csv_writer_actually_applies_it():
    """A formatter nobody calls fixes nothing. Pins the call site rather than
    just the function."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src" / "dba_slack_bot"
           / "executor.py").read_text(encoding="utf-8")
    assert "cell_format.row_formatter(getattr(cur, \"description\", None))" in src
    assert "if fmt_row is not None:" in src
    # and it must run BEFORE the CSV row is serialized
    i_fmt = src.index("r = fmt_row(r)")
    i_write = src.index("csv.writer(row_buf).writerow(r)")
    assert i_fmt < i_write


# ---------- datetimeoffset: the driver could not read it at all ----------

def test_datetimeoffset_parses_from_the_real_wire_bytes():
    """These 20 bytes were captured from the live SQL Server (2026-07-30) in the
    same session that found the failure, and the server rendered them as
    `2026-07-30 15:29:39.3971278 +03:00`. Before the converter, ANY query
    touching a `datetimeoffset` column raised
    `ODBC SQL type -155 is not yet supported` and returned nothing at all."""
    from dba_slack_bot import mssql_exec
    raw = bytes.fromhex("ea0707001e000f001d00270078b0ab1703000000")
    got = mssql_exec.parse_datetimeoffset(raw)
    assert got.year == 2026 and got.month == 7 and got.day == 30
    assert (got.hour, got.minute, got.second) == (15, 29, 39)
    assert got.microsecond == 397127          # 7th digit is lost in transport
    assert got.utcoffset() == timedelta(hours=3)


def test_datetimeoffset_handles_a_negative_zone():
    """SQL Server signs BOTH offset fields, so -05:30 arrives as (-5, -30).
    Verified live with TODATETIMEOFFSET(..., -330)."""
    import struct
    from dba_slack_bot import mssql_exec
    raw = struct.pack("<6hI2h", 2026, 3, 1, 8, 9, 10, 123456700, -5, -30)
    got = mssql_exec.parse_datetimeoffset(raw)
    assert got.utcoffset() == timedelta(hours=-5, minutes=-30)
    assert got.microsecond == 123456


def test_datetimeoffset_null_stays_null():
    from dba_slack_bot import mssql_exec
    assert mssql_exec.parse_datetimeoffset(None) is None


def test_a_malformed_datetimeoffset_does_not_kill_the_result():
    """Fail-visible, not fail-closed: raising here would reproduce the very bug
    the converter fixes — a query that already produced rows dying at fetch."""
    from dba_slack_bot import mssql_exec
    got = mssql_exec.parse_datetimeoffset(b"\x01\x02\x03")
    assert got == "010203"


def test_the_converter_is_registered_on_every_connection():
    """Per-cursor registration would leave whichever path forgot it broken, so
    this pins the connection-level hook."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src" / "dba_slack_bot"
           / "mssql_exec.py").read_text(encoding="utf-8")
    assert "conn.add_output_converter(SQL_SS_TIMESTAMPOFFSET, parse_datetimeoffset)" in src
    assert "SQL_SS_TIMESTAMPOFFSET = -155" in src


def test_offset_and_trimming_compose():
    """The two fixes from this session meet on `datetimeoffset(3)`: the padding
    goes, the zone stays. Measured live as `…10.421+03:00`."""
    from datetime import timezone as _tz
    v = datetime(2026, 7, 30, 15, 31, 10, 421000, tzinfo=_tz(timedelta(hours=3)))
    assert cell_format.format_temporal(v, 3) == "2026-07-30 15:31:10.421+03:00"


# ---------- sql_variant + UDTs: the same "type not supported" family ----------

def test_sql_variant_needs_only_permission_not_parsing():
    """Measured live 2026-07-30: with a converter registered, the driver hands
    sql_variant over ALREADY DECODED — pyodbc was only refusing to route the
    type, not failing to read it. So passing the value straight through is a
    complete fix rather than a workaround."""
    from dba_slack_bot import mssql_exec
    from decimal import Decimal as D
    assert mssql_exec.passthrough_or_hex("merhaba") == "merhaba"
    assert mssql_exec.passthrough_or_hex(D("1234.56")) == D("1234.56")
    assert mssql_exec.passthrough_or_hex(42) == 42
    assert mssql_exec.passthrough_or_hex(None) is None


def test_an_opaque_udt_becomes_obvious_hex_not_a_plausible_guess():
    """hierarchyid and geography are SQL Server's own serialisation, not standard
    WKB. A wrong parse would put a plausible, WRONG coordinate in front of
    someone — worse than an obviously-opaque value that says "ask the server to
    convert this" (col.STAsText() / col.ToString())."""
    from dba_slack_bot import mssql_exec
    assert mssql_exec.passthrough_or_hex(b"\x5b\x5e") == "0x5B5E"
    assert mssql_exec.passthrough_or_hex(bytearray(b"\xe6\x10")) == "0xE610"
    assert mssql_exec.passthrough_or_hex(memoryview(b"\x00\xff")) == "0x00FF"


def test_both_codes_are_registered_so_no_query_can_die_on_them():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src" / "dba_slack_bot"
           / "mssql_exec.py").read_text(encoding="utf-8")
    assert "SQL_VARIANT = -16" in src and "SQL_SS_UDT = -151" in src
    assert "conn.add_output_converter(SQL_VARIANT, passthrough_or_hex)" in src
    assert "conn.add_output_converter(SQL_SS_UDT, passthrough_or_hex)" in src
