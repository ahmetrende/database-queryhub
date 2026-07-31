"""SET LOCAL value validation — an allow-listed param can't be abused by value.

Name allow-listing alone let `statement_timeout=0` disable the timeout and
`work_mem='100GB'` invite an OOM. `_validate_set` now also bounds the value.
"""
import pytest

from queryhub import query_safety as qs

ALLOWED = qs.SET_ALLOWED_DEFAULT


@pytest.mark.parametrize("sql", [
    "SET LOCAL statement_timeout = 0",           # 0 = disable → reject
    "SET statement_timeout = 999999",            # > 10min cap
    "SET lock_timeout = 0",
    "SET work_mem = '100GB'",                    # OOM
    "SET work_mem = 0",
    "SET enable_seqscan = maybe",                # not a boolean
    "SET default_statistics_target = 999999",    # out of range
    "SET random_page_cost = -5",                 # negative
    "SET work_mem = 'lots'",                      # unparseable size
])
def test_dangerous_set_values_rejected(sql):
    ok, _, err = qs._validate_set(sql, set(ALLOWED))
    assert ok is False and err


@pytest.mark.parametrize("sql", [
    "SET statement_timeout = 30000",
    "SET LOCAL statement_timeout = '30s'",
    "SET work_mem = '64MB'",
    "SET work_mem = 65536",
    "SET enable_seqscan = off",
    "SET jit = on",
    "SET default_statistics_target = 200",
    "SET random_page_cost = 1.1",
])
def test_reasonable_set_values_pass(sql):
    ok, rewritten, err = qs._validate_set(sql, set(ALLOWED))
    assert ok is True, err
    assert rewritten.upper().startswith("SET LOCAL ")   # still LOCAL-rewritten


def test_value_parsers():
    assert qs._parse_pg_duration_ms("30000") == 30000
    assert qs._parse_pg_duration_ms("'30s'") == 30000
    assert qs._parse_pg_duration_ms("2min") == 120000
    assert qs._parse_pg_duration_ms("junk") is None
    assert qs._parse_pg_size_kb("65536") == 65536
    assert qs._parse_pg_size_kb("'64MB'") == 65536
    assert qs._parse_pg_size_kb("1GB") == 1024 * 1024
    assert qs._parse_pg_size_kb("huge") is None
