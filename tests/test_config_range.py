"""C18: bot_config int values are range-checked on write — no negatives,
and no zero for keys where zero would disable a safety limit."""
from queryhub.web import config_admin as ca


def test_negative_int_rejected():
    assert ca._coerce("-5", "int", "10", key="max_rows") is None


def test_zero_rejected_for_safety_limit_keys():
    assert ca._coerce("0", "int", "300", key="query_timeout_sec") is None
    assert ca._coerce("0", "int", "900", key="execution_lease_sec") is None


def test_zero_allowed_for_ordinary_int():
    # A cost/threshold that may legitimately be zero.
    assert ca._coerce("0", "int", "5", key="cost_dba_hourly_usd") == "0"


def test_positive_value_passes():
    assert ca._coerce("120", "int", "300", key="query_timeout_sec") == "120"


def test_non_numeric_rejected():
    assert ca._coerce("abc", "int", "300", key="query_timeout_sec") is None
