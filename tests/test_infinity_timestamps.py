"""`infinity` timestamps must not lose the whole result.

`SELECT * FROM pg_roles` failed with "Timestamp too large (after year 10K):
'infinity'" (request #4892). `rolvaliduntil` is `infinity` for every role
created without VALID UNTIL — three of them on the server this was measured on
— and psycopg raises while READING the row, so one unrepresentable cell took
the entire result with it.

The loaders return the literal text for the two infinite values. Text, not
`datetime.max`: a result is delivered as CSV/XLSX, so the reader gets the word
Postgres uses instead of `9999-12-31`, which means something else.
"""
import datetime

from queryhub import pg_types

TSTZ_OID = 1184
TS_OID = 1114
DATE_OID = 1082


def test_infinite_timestamptz_comes_back_as_text():
    ld = pg_types.InfSafeTimestamptzLoader(TSTZ_OID, None)
    assert ld.load(b"infinity") == "infinity"
    assert ld.load(b"-infinity") == "-infinity"


def test_a_real_timestamptz_still_loads_as_a_datetime():
    ld = pg_types.InfSafeTimestamptzLoader(TSTZ_OID, None)
    v = ld.load(b"2026-08-21 22:14:03.5+00")
    assert isinstance(v, datetime.datetime)
    assert (v.year, v.month, v.day) == (2026, 8, 21)


def test_timestamp_and_date_are_covered_too():
    # pg_roles is timestamptz, but `infinity` is legal in all three types and
    # any of them would have taken the result down the same way.
    assert pg_types.InfSafeTimestampLoader(TS_OID, None).load(b"infinity") \
        == "infinity"
    assert pg_types.InfSafeDateLoader(DATE_OID, None).load(b"-infinity") \
        == "-infinity"
    assert isinstance(
        pg_types.InfSafeDateLoader(DATE_OID, None).load(b"2026-08-21"),
        datetime.date)


def test_a_memoryview_is_handled():
    # psycopg hands the loader a buffer, not always bytes.
    ld = pg_types.InfSafeTimestamptzLoader(TSTZ_OID, None)
    assert ld.load(memoryview(b"infinity")) == "infinity"


def test_registration_covers_the_three_types():
    seen = []

    class _Adapters:
        def register_loader(self, name, loader):
            seen.append((name, loader))

    class _Conn:
        adapters = _Adapters()

    pg_types.register_infinity_safe_loaders(_Conn())
    assert [n for n, _ in seen] == ["date", "timestamp", "timestamptz"]


def test_registration_never_raises():
    # A formatting nicety must not be the reason a query fails to run.
    class _Conn:
        @property
        def adapters(self):
            raise RuntimeError("psycopg changed shape")

    pg_types.register_infinity_safe_loaders(_Conn())   # no exception
