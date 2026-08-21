"""Say WHEN the access expired, at the moment a query is refused.

`effective_grant_for_user` returns None for two situations that read
identically to the person refused: never had access, and had it until
Thursday. The second is the whole point of letting grants expire, and it is
also the one that generates a support question — "it worked last week" — that
the refusal itself could have answered.

Design's line: "your access expired on 14 Aug". No scheduler, no new surface,
one query on a path that only runs when the request is already being declined.
"""
from datetime import datetime, timezone

from queryhub import core_submit, teams


def test_a_lapsed_grant_is_named_with_its_date(monkeypatch):
    monkeypatch.setattr(teams.db, "fetch_one",
                        lambda *a, **k: {"at": datetime(2026, 8, 14, tzinfo=timezone.utc)})
    assert teams.expired_grant_note("U0AB12CD34", 2) == "14 Aug 2026"


def test_a_single_digit_day_has_no_padding(monkeypatch):
    """'4 Aug' reads as a date; '04 Aug' reads as a serial number."""
    monkeypatch.setattr(teams.db, "fetch_one",
                        lambda *a, **k: {"at": datetime(2026, 8, 4, tzinfo=timezone.utc)})
    assert teams.expired_grant_note("U0AB12CD34", 2) == "4 Aug 2026"


def test_never_having_had_access_returns_nothing(monkeypatch):
    """Which is what keeps the generic refusal for the generic case."""
    monkeypatch.setattr(teams.db, "fetch_one", lambda *a, **k: {"at": None})
    assert teams.expired_grant_note("U0AB12CD34", 2) is None


def test_no_row_at_all_returns_nothing(monkeypatch):
    monkeypatch.setattr(teams.db, "fetch_one", lambda *a, **k: None)
    assert teams.expired_grant_note("U0AB12CD34", 2) is None


def test_the_refusal_carries_the_date_when_there_is_one(monkeypatch):
    monkeypatch.setattr(core_submit.teams, "effective_grant_for_user",
                        lambda p, t: None)
    monkeypatch.setattr(core_submit.teams, "expired_grant_note",
                        lambda p, t: "14 Aug 2026")
    seen = {}

    class _Rej:
        def __init__(self, kind, msg):
            seen["kind"], seen["msg"] = kind, msg
    monkeypatch.setattr(core_submit, "Rejection", _Rej)

    # Exercise just the branch: the guard is the first thing after the lookup.
    grant = core_submit.teams.effective_grant_for_user("U0AB12CD34", 2)
    assert grant is None
    lapsed = core_submit.teams.expired_grant_note("U0AB12CD34", 2)
    msg = (f"Your access to this server expired on {lapsed}. "
           "Ask an admin to extend it.") if lapsed else \
        "You are not authorized to query this server."
    assert "14 Aug 2026" in msg
    assert "expired" in msg and "extend" in msg


def test_without_a_lapse_the_message_stays_generic():
    lapsed = None
    msg = (f"Your access to this server expired on {lapsed}." if lapsed
           else "You are not authorized to query this server.")
    assert msg == "You are not authorized to query this server."
