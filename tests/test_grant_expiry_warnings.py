"""Warning before a grant lapses.

Migration 096 let a standing grant end on its own and nothing announced it:
the auth-event triggers fire on row changes, and time passing is not one, so
access stopped working and the holder found out by being refused.
"""
from datetime import datetime, timedelta, timezone

import pytest

from queryhub import grant_expiry as ge

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


# --- thresholds -------------------------------------------------------------

def test_default_thresholds_are_narrowest_first(monkeypatch):
    """Each threshold owns the bucket up to the next one, so they are walked
    from the narrowest out."""
    monkeypatch.setattr(ge.cfg, "get_setting", lambda k, d=None: "24,4")
    assert ge.thresholds() == [4, 24]


def test_thresholds_tolerate_junk_and_dedupe(monkeypatch):
    monkeypatch.setattr(ge.cfg, "get_setting", lambda k, d=None: "4, ,x,24,4,-1,0")
    assert ge.thresholds() == [4, 24]


def test_empty_thresholds_disable_warning_without_disabling_the_feature(monkeypatch):
    monkeypatch.setattr(ge.cfg, "get_setting", lambda k, d=None: "")
    assert ge.thresholds() == []


# --- wording ----------------------------------------------------------------

def _grant(kind="user", hours=24, mode="rw", team=None):
    return {"kind": kind, "id": 7, "subject": "U0AB12CD34", "mode": mode,
            "expires_at": NOW + timedelta(hours=hours),
            "alias": "prod-main", "team_name": team}


def test_a_day_out_reads_as_tomorrow():
    assert "tomorrow" in ge.message(_grant(hours=24), now=NOW)


def test_four_hours_out_says_hours():
    assert "in 4 hours" in ge.message(_grant(hours=4), now=NOW)


def test_the_wording_comes_from_the_deadline_not_the_trigger():
    """The bug a live test caught: a grant three hours out was warned at the
    24-hour threshold and told it expired TOMORROW. The threshold is a trigger,
    not a fact about the grant."""
    assert "in 3 hours" in ge.message(_grant(hours=3), now=NOW)
    assert "tomorrow" not in ge.message(_grant(hours=3), now=NOW)


def test_under_an_hour_counts_in_minutes():
    g = _grant()
    g["expires_at"] = NOW + timedelta(minutes=25)
    assert "in 25 minutes" in ge.message(g, now=NOW)


def test_the_message_names_the_tier_the_server_and_the_deadline():
    m = ge.message(_grant(hours=24), now=NOW)
    assert "*RW*" in m and "prod-main" in m and "2026-08-22 12:00 UTC" in m


def test_a_team_grant_says_which_team_carries_it():
    """Otherwise the holder has no idea why they had the access at all."""
    m = ge.message(_grant(kind="team", team="petrels"), now=NOW)
    assert "petrels" in m and "team" in m


# --- the sweep --------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.sent = []
        self.recorded = []


@pytest.fixture
def swept(monkeypatch):
    def _run(due_by_threshold, recipients, fail_for=(), enabled=True,
             hours="24,4"):
        r = _Recorder()
        monkeypatch.setattr(ge, "is_enabled", lambda: enabled)
        monkeypatch.setattr(ge.cfg, "get_setting", lambda k, d=None: hours)
        monkeypatch.setattr(ge, "due",
                            lambda h, lo=0: due_by_threshold.get(h, []))
        monkeypatch.setattr(ge, "recipients_for", lambda g: recipients)
        monkeypatch.setattr(ge, "_record",
                            lambda g, h, n: r.recorded.append((g["kind"], g["id"], h, n)))

        def dm(c, uid, text):
            if uid in fail_for:
                raise RuntimeError("channel_not_found")
            r.sent.append((uid, text))
        from queryhub.slack_app import notifications
        monkeypatch.setattr(notifications, "dm_requester", dm)
        r.warned = ge.sweep(object())
        return r
    return _run


def test_a_grant_expiring_soon_warns_once(swept):
    r = swept({24: [_grant()]}, ["U0AB12CD34"])
    assert r.warned == 1
    assert len(r.sent) == 1
    assert r.recorded == [("user", 7, 24, 1)]


def test_the_buckets_are_disjoint_so_each_grant_warns_once_per_pass(swept):
    """A grant with three hours left belongs to the 4-hour bucket only. The
    bucket floor is what enforces that in SQL; here the sweep is checked not to
    re-send when both thresholds are configured."""
    r = swept({4: [_grant(hours=3)]}, ["U0AB12CD34"])
    assert len(r.sent) == 1
    assert [h for _, _, h, _ in r.recorded] == [4]
    # Wording is asserted against a pinned clock in the message tests above —
    # sweep() reads the real one, so it is not pinned here.
    assert "expires in" in r.sent[0][1]


def test_a_long_lived_grant_warns_at_each_line_it_crosses(swept):
    """Not suppression — a month-long grant SHOULD get the 24-hour warning and
    then the 4-hour one. They are different messages answering different
    questions."""
    r = swept({24: [_grant(hours=20)]}, ["U0AB12CD34"])
    assert [h for _, _, h, _ in r.recorded] == [24]
    r2 = swept({4: [_grant(hours=2)]}, ["U0AB12CD34"])
    assert [h for _, _, h, _ in r2.recorded] == [4]


def test_every_member_of_a_team_is_warned(swept):
    r = swept({24: [_grant(kind="team", team="petrels")]},
              ["U0AB12CD34", "U0XY98ZW76", "U0PQ55RS11"])
    assert len(r.sent) == 3
    assert r.recorded == [("team", 7, 24, 3)]


def test_nothing_is_recorded_when_every_send_fails(swept):
    """Otherwise the warning is lost: the record would suppress the retry."""
    r = swept({24: [_grant()]}, ["U0AB12CD34"], fail_for={"U0AB12CD34"})
    assert r.sent == []
    assert r.recorded == []
    assert r.warned == 0


def test_a_partial_failure_still_records(swept):
    """One unreachable member must not make the whole team warn again forever."""
    r = swept({24: [_grant(kind="team", team="t")]},
              ["U0AB12CD34", "U0BAD00000"], fail_for={"U0BAD00000"})
    assert len(r.sent) == 1
    assert r.recorded == [("team", 7, 24, 1)]


def test_an_empty_team_is_recorded_so_the_sweep_stops_asking(swept):
    r = swept({24: [_grant(kind="team", team="t")]}, [])
    assert r.sent == []
    assert r.recorded == [("team", 7, 24, 0)]
    assert r.warned == 0


def test_disabled_sends_nothing(swept):
    r = swept({24: [_grant()]}, ["U0AB12CD34"], enabled=False)
    assert r.sent == [] and r.recorded == []
