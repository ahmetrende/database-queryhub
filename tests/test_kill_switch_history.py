"""The history of the kill switch: one entry per pause, newest first.

PLA-1592 item U4b: `GET /api/admin/kill/history` returns
`[{startedAt, by, message, endedAt, endedBy, cancelledCount}]`. The sources are
two audit actions. A `kill_switch_set` row with `enabled = true` starts a
pause, and the next one with `enabled = false` ends it. A pause that is still
on has no end. The `kill_switch_cancelled` row that the same person writes
after the switch went on carries the count. A pause without such a row shows
null, never 0. A pause from before the count existed is one example. 0 would
say that somebody counted and found nothing.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from queryhub import admins, kill_switch
from queryhub import db as dbm
from queryhub.web import mapping, routes_admin as ra

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
JORDAN = ("U0EXAMPLE001", "Jordan Ray")
CASEY = ("U0EXAMPLE003", "Casey Lee")


def _set(i, minutes, enabled, who=JORDAN, **details):
    return {"id": i, "action": "kill_switch_set", "actor_slack_id": who[0],
            "actor_name": who[1], "created_at": T0 + timedelta(minutes=minutes),
            "details": {"enabled": enabled, **details}}


def _cancelled(i, minutes, count, who=JORDAN):
    return {"id": i, "action": "kill_switch_cancelled", "actor_slack_id": who[0],
            "actor_name": who[1], "created_at": T0 + timedelta(minutes=minutes),
            "details": {"cancelledCount": count}}


def _at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


# Oldest first, as the audit table holds them.
ROWS = [
    _set(1, 0, True),                                     # before message and count
    _set(2, 30, False, who=CASEY),
    _set(3, 60, True, message="Deploying the ledger fix."),
    _cancelled(4, 60, 2),
    _set(5, 75, False),
    _set(6, 120, True, who=CASEY, message="Disk alarm."),
    _cancelled(7, 120, 0, who=CASEY),
]


def test_no_rows_is_no_pauses():
    assert kill_switch.periods([]) == []


def test_three_pauses_newest_first():
    assert kill_switch.periods(ROWS) == [
        {"startedAt": _at(120), "by": "Casey Lee", "message": "Disk alarm.",
         "endedAt": None, "endedBy": None, "cancelledCount": 0},
        {"startedAt": _at(60), "by": "Jordan Ray", "message": "Deploying the ledger fix.",
         "endedAt": _at(75), "endedBy": "Jordan Ray", "cancelledCount": 2},
        {"startedAt": _at(0), "by": "Jordan Ray", "message": None,
         "endedAt": _at(30), "endedBy": "Casey Lee", "cancelledCount": None},
    ]


def test_a_pause_without_a_count_row_says_null_never_zero():
    (old,) = kill_switch.periods(ROWS[:2])
    assert old["message"] is None and old["cancelledCount"] is None


def test_on_while_on_each_pause_keeps_its_own_count_and_one_off_ends_both():
    rows = [_set(1, 0, True, message="a"), _cancelled(2, 0, 1),
            _set(3, 5, True, who=CASEY, message="b"), _cancelled(4, 5, 0, who=CASEY),
            _set(5, 9, False, who=CASEY)]
    got = kill_switch.periods(rows)
    assert [(p["message"], p["cancelledCount"]) for p in got] == [("b", 0), ("a", 1)]
    assert {(p["endedAt"], p["endedBy"]) for p in got} == {(_at(9), "Casey Lee")}


def test_a_resume_before_the_cancels_end_keeps_the_count():
    """The cancels can take seconds on a hung target, and somebody else can set
    the switch off meanwhile. The count still belongs to the pause."""
    rows = [_set(1, 0, True), _set(2, 1, False, who=CASEY), _cancelled(3, 1, 3)]
    (p,) = kill_switch.periods(rows)
    assert (p["endedBy"], p["cancelledCount"]) == ("Casey Lee", 3)


def test_two_people_at_once_get_their_own_counts():
    rows = [_set(1, 0, True), _set(2, 0, True, who=CASEY),
            _cancelled(3, 1, 4), _cancelled(4, 1, 0, who=CASEY)]
    got = {p["by"]: p["cancelledCount"] for p in kill_switch.periods(rows)}
    assert got == {"Jordan Ray": 4, "Casey Lee": 0}


def test_an_unknown_count_stays_null_and_a_second_row_does_not_land():
    rows = [_set(1, 0, True), _cancelled(2, 0, None), _cancelled(3, 1, 7)]
    (p,) = kill_switch.periods(rows)
    assert p["cancelledCount"] is None


def test_a_count_row_of_somebody_else_lands_nowhere():
    (p,) = kill_switch.periods([_set(1, 0, True), _cancelled(2, 0, 4, who=CASEY)])
    assert p["cancelledCount"] is None


def test_an_off_with_no_pause_ends_nothing():
    rows = [_set(1, 0, False), _set(2, 5, True), _set(3, 9, False),
            _set(4, 12, False, who=CASEY)]
    (only,) = kill_switch.periods(rows)
    assert (only["endedAt"], only["endedBy"]) == (_at(9), "Jordan Ray")


def test_a_row_that_does_not_say_on_or_off_ends_nothing():
    rows = [_set(1, 0, True), {**_set(2, 5, True), "details": {"via": "slack"}},
            _set(3, 9, False)]
    (only,) = kill_switch.periods(rows)
    assert only["endedAt"] == _at(9)


@pytest.mark.parametrize("on,off", [(True, False), ("true", "false"), ("TRUE", "False")])
def test_enabled_reads_as_get_kill_reads_it(on, off):
    """GET /kill asks `details->>'enabled' = 'true'`. The history agrees."""
    (p,) = kill_switch.periods([_set(1, 0, on), _set(2, 5, off)])
    assert p["endedAt"] == _at(5)


def test_slack_rows_and_text_details_pair_the_same_way():
    rows = [_set(1, 0, True, **{"from": "off", "to": "on", "via": "slack", "message": "m"}),
            {**_cancelled(2, 0, 0), "details": '{"cancelledCount": 1}'},
            {**_set(3, 5, False), "details": '{"enabled": false, "via": "slack"}'}]
    (p,) = kill_switch.periods(rows)
    assert (p["message"], p["cancelledCount"], p["endedAt"]) == ("m", 1, _at(5))


def test_the_actor_falls_back_to_the_id():
    (p,) = kill_switch.periods([{**_set(1, 0, True), "actor_name": None}])
    assert p["by"] == "U0EXAMPLE001"


def test_history_reads_both_actions_newest_rows_first_and_pairs_them_in_order(
        monkeypatch):
    seen = {}

    def fetch_all(sql, params=None):
        seen["sql"], seen["params"] = " ".join(sql.split()), params
        return list(reversed(ROWS))          # ORDER BY id DESC
    monkeypatch.setattr(dbm, "fetch_all", fetch_all)
    got = kill_switch.history()
    assert "WHERE action IN (%s, %s)" in seen["sql"]
    assert "ORDER BY id DESC LIMIT %s" in seen["sql"]
    assert seen["params"] == ("kill_switch_set", "kill_switch_cancelled",
                              kill_switch.HISTORY_ROWS)
    assert [(p["startedAt"], p["cancelledCount"]) for p in got] == [
        (_at(120), 0), (_at(60), 2), (_at(0), None)]


def test_the_audit_screen_draws_the_count_row_as_part_of_the_switch():
    assert mapping.audit_kind("kill_switch_cancelled") == "kill"
    entry = mapping.admin_audit_entry(
        {"id": 7, "action": "kill_switch_cancelled", "actor_name": "Jordan Ray",
         "details": {"cancelledCount": 2}, "created_at": T0},
        "kill", lambda tid: None)
    assert entry["event"] == "Kill switch stopped running reads"


# --- the route --------------------------------------------------------------------

@pytest.fixture
def route(monkeypatch):
    monkeypatch.setattr(dbm, "fetch_all", lambda sql, params=None: list(reversed(ROWS)))
    monkeypatch.setattr(admins, "is_admin", lambda pid: pid in (JORDAN[0], CASEY[0]))
    monkeypatch.setattr(admins, "is_super_admin", lambda pid: pid == JORDAN[0])


def test_any_admin_reads_the_history_as_a_list(route):
    got = ra.kill_history(claims={"sub": CASEY[0]})      # not a super-admin
    assert isinstance(got, list) and len(got) == 3
    assert set(got[0]) == {"startedAt", "by", "message", "endedAt", "endedBy",
                           "cancelledCount"}


def test_a_person_who_is_not_an_admin_is_refused(route):
    with pytest.raises(HTTPException) as e:
        ra.kill_history(claims={"sub": "U0EXAMPLE002"})
    assert e.value.status_code == 403


def test_the_route_answers_a_json_list_at_its_path(route):
    from fastapi.testclient import TestClient
    from queryhub.web import app as web_app, deps
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {"sub": CASEY[0]}
    res = TestClient(app).get("/api/admin/kill/history")
    assert res.status_code == 200
    assert [p["startedAt"] for p in res.json()] == [_at(120), _at(60), _at(0)]
