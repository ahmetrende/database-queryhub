"""How long an auto-approve rule made in the app may last, and which tier it has.

`auto_approve_limits.check_end` and `check_tier` are the functions that every
application path calls before it writes an auto-approve row. These tests pin
their answers, the `auto_approve_max_days` setting that feeds the first, and
the System configuration screen's refusal of a value that would switch the
limit off.

No database. The conftest makes every config read return its default. A test
that needs another value patches `cfg.get_setting` itself.
"""
from datetime import datetime, timedelta, timezone

import pytest

from queryhub import auto_approve_limits as lim
from queryhub import config as cfg
from queryhub.web import config_admin

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def _setting(monkeypatch, value):
    """Make `auto_approve_max_days` read as `value`. An Exception is raised."""
    def fake(key, default=None):
        if key != "auto_approve_max_days":
            return default
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(cfg, "get_setting", fake)


# --- max_days ----------------------------------------------------------------

def test_the_default_is_ninety_days():
    assert lim.max_days() == 90


@pytest.mark.parametrize("stored,want", [("1", 1), ("30", 30), ("90", 90), ("365", 365)])
def test_a_stored_value_in_range_is_used(monkeypatch, stored, want):
    _setting(monkeypatch, stored)
    assert lim.max_days() == want


@pytest.mark.parametrize("stored", ["0", "-1", "366", "9999", "", "abc", "12.5", " "])
def test_a_stored_value_that_is_not_a_whole_number_in_range_counts_as_ninety(monkeypatch, stored):
    """The limit must not turn into "no limit" because somebody edited the row."""
    _setting(monkeypatch, stored)
    assert lim.max_days() == 90


def test_a_failed_read_counts_as_ninety(monkeypatch):
    _setting(monkeypatch, RuntimeError("the control database is down"))
    assert lim.max_days() == 90


# --- check_end: the refusals -------------------------------------------------

def _refused(code, **kw):
    with pytest.raises(lim.EndRefused) as e:
        lim.check_end(now=NOW, days=90, **kw)
    assert e.value.code == code
    return e.value


@pytest.mark.parametrize("kw", [
    {}, {"expires_at": None}, {"expires_at": ""}, {"expires_at": "  "},
    {"expires_in_minutes": 0}, {"expires_in_minutes": None},
    {"expires_at": None, "expires_in_minutes": 0},
])
def test_no_end_is_refused(kw):
    e = _refused("end_required", **kw)
    assert e.message == "An auto-approve rule needs an end date, at most 90 days from now."


def test_an_end_later_than_the_limit_is_refused():
    e = _refused("end_too_far", expires_at=NOW + 91 * DAY)
    assert "at most 90 days from now" in e.message
    _refused("end_too_far", expires_at=(NOW + 91 * DAY).isoformat())
    _refused("end_too_far", expires_in_minutes=90 * 1440 + 1)
    _refused("end_too_far", expires_in_minutes=10 ** 15)      # no overflow, no pass


def test_the_limit_in_the_message_is_the_limit_in_force():
    with pytest.raises(lim.EndRefused) as e:
        lim.check_end(NOW + 31 * DAY, now=NOW, days=30)
    assert "at most 30 days" in e.value.message


@pytest.mark.parametrize("kw", [
    {"expires_at": NOW}, {"expires_at": NOW - DAY},
    {"expires_at": (NOW - DAY).isoformat()}, {"expires_in_minutes": -5},
])
def test_an_end_that_is_not_ahead_is_refused(kw):
    _refused("end_in_past", **kw)


def test_a_string_that_is_not_a_timestamp_is_refused():
    e = _refused("bad_request", expires_at="next friday")
    assert "ISO-8601" in e.message


def test_the_refusals_are_value_errors():
    """`grants.grant_many` callers already turn a ValueError into HTTP 400."""
    assert issubclass(lim.EndRefused, ValueError)
    assert issubclass(lim.TierRefused, ValueError)
    assert issubclass(lim.EndRefused, lim.RuleRefused)
    assert issubclass(lim.TierRefused, lim.RuleRefused)


# --- check_end: the accepted ends --------------------------------------------

@pytest.mark.parametrize("end", [
    NOW + timedelta(minutes=1), NOW + 30 * DAY, NOW + 90 * DAY,
    # The room for a browser clock that runs a little ahead.
    NOW + 90 * DAY + timedelta(minutes=4),
])
def test_an_end_within_the_limit_is_accepted_and_returned(end):
    assert lim.check_end(end, now=NOW, days=90) == end
    assert lim.check_end(end.isoformat().replace("+00:00", "Z"), now=NOW, days=90) == end


def test_a_string_with_no_zone_is_read_as_utc():
    end = lim.check_end("2026-11-24T12:00:00", now=NOW, days=90)
    assert end == datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def test_minutes_are_counted_from_now_and_win_over_a_date():
    end = lim.check_end(NOW + 400 * DAY, expires_in_minutes=60, now=NOW, days=90)
    assert end == NOW + timedelta(minutes=60)


def test_minutes_up_to_the_limit_are_accepted_with_no_room():
    assert lim.check_end(expires_in_minutes=90 * 1440, now=NOW, days=90) == NOW + 90 * DAY


def test_the_end_is_read_against_the_setting_when_days_is_left_out(monkeypatch):
    _setting(monkeypatch, "7")
    with pytest.raises(lim.EndRefused) as e:
        lim.check_end(datetime.now(timezone.utc) + 8 * DAY)
    assert e.value.code == "end_too_far"
    lim.check_end(datetime.now(timezone.utc) + 6 * DAY)


# --- check_tier --------------------------------------------------------------

@pytest.mark.parametrize("tier", ["ddl", "DDL", " ddl ", "admin", "", None, 0])
def test_a_tier_other_than_ro_or_rw_is_refused(tier):
    with pytest.raises(lim.TierRefused) as e:
        lim.check_tier(tier)
    assert e.value.code == "tier_not_allowed"
    assert e.value.message == "An auto-approve rule can be RO or RW. Schema changes always need a DBA."


@pytest.mark.parametrize("tier,want", [("ro", "ro"), ("RW", "rw"), (" Ro ", "ro")])
def test_ro_and_rw_are_accepted(tier, want):
    assert lim.check_tier(tier) == want


# --- copied_end --------------------------------------------------------------

def test_a_copy_of_a_rule_with_no_end_gets_the_limit():
    assert lim.copied_end(None, now=NOW, days=90) == NOW + 90 * DAY


def test_a_copy_keeps_an_end_within_the_limit():
    end = NOW + 10 * DAY
    assert lim.copied_end(end, now=NOW, days=90) == end


def test_a_copy_cannot_end_later_than_the_limit():
    assert lim.copied_end(NOW + 400 * DAY, now=NOW, days=90) == NOW + 90 * DAY
    # A naive datetime from a driver is read as UTC.
    assert lim.copied_end((NOW + 400 * DAY).replace(tzinfo=None), now=NOW, days=90) == NOW + 90 * DAY


# --- the System configuration screen ------------------------------------------

KEY = "auto_approve_max_days"


@pytest.mark.parametrize("raw", [0, "0", "", "  ", None, -1, "-1", 366, "366", 1000, "abc",
                                 "nan", "inf", "-inf"])
def test_the_screen_refuses_a_value_that_would_switch_the_limit_off(raw):
    assert config_admin._coerce(raw, "int", "90", key=KEY) is None


@pytest.mark.parametrize("raw,want", [(1, "1"), ("1", "1"), (90, "90"), ("90", "90"),
                                      (365, "365"), (" 30 ", "30"), ("45.0", "45")])
def test_the_screen_accepts_a_whole_number_from_1_to_365(raw, want):
    assert config_admin._coerce(raw, "int", "90", key=KEY) == want


def test_another_key_keeps_its_own_rules():
    assert config_admin._coerce(366, "int", "10", key="max_rows") == "366"
    assert config_admin._coerce(0, "int", "10", key="cost_other_monthly_usd") == "0"
    assert config_admin._coerce(0, "int", "10", key="max_rows") is None


class _Cur:
    """Enough of a cursor for `apply_config`."""

    def __init__(self, rows):
        self.rows, self.updates = rows, []

    def execute(self, sql, params=()):
        if sql.startswith("UPDATE"):
            self.updates.append(params)

    def fetchall(self):
        return self.rows


def _store(value="90"):
    return _Cur([{"key": KEY, "value": value}])


@pytest.mark.parametrize("raw", [0, "", -1, 366, "abc"])
def test_a_save_with_a_refused_value_is_an_error_and_stores_nothing(raw):
    cur = _store()
    with pytest.raises(ValueError, match="auto_approve_max_days must be a whole number from 1 to 365"):
        config_admin.apply_config({KEY: raw}, cur)
    assert cur.updates == []


@pytest.mark.parametrize("raw", [1, 30, 365])
def test_a_save_with_a_value_in_range_is_stored(raw):
    cur = _store()
    applied = config_admin.apply_config({KEY: raw}, cur)
    assert applied == [{"key": KEY, "from": "90", "to": str(raw)}]
    assert cur.updates == [(str(raw), KEY)]


def test_the_key_is_on_the_screen_as_a_number_in_the_grants_group():
    assert config_admin._TYPES[KEY] == "int"
    group = next(g for g in config_admin._GROUPS if g[0] == "grants")
    assert KEY in group[2]
