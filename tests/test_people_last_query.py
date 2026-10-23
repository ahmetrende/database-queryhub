"""GET /admin/people: when each person last sent a query (PLA-1592 U6).

The People screen shows a "Last query" column. The value comes from ONE grouped
read joined to the whole list: a query per person is the shape that is fine
for ten people and slow for a thousand. A draft is a reserved id for an editor
tab that someone opened, so it is not a query and does not count.
tests/test_integration_queue_metrics.py runs the statement on a real database.
"""
from datetime import datetime, timezone

from queryhub.web import routes_admin as ra

WHEN = datetime(2026, 10, 21, 14, 12, tzinfo=timezone.utc)


def _wire(monkeypatch, rows):
    calls = []
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, s: "U0ADMIN")
    # The route reads the caller's level to decide how much of a row they get.
    # These tests hold the full row, so the caller is a super-admin.
    monkeypatch.setattr(ra.admins, "is_super_admin", lambda uid: True)
    monkeypatch.setattr(ra.db, "fetch_all",
                        lambda sql, *a, **k: calls.append(" ".join(sql.split())) or rows)

    def no_single_reads(*a, **k):
        raise AssertionError("a per-person read")
    monkeypatch.setattr(ra.db, "fetch_one", no_single_reads)
    return calls


def test_each_person_carries_the_time_of_their_last_query(monkeypatch):
    _wire(monkeypatch, [
        {"slack_user_id": "U0EXAMPLE001", "name": "Jordan Ray", "enabled": True,
         "kind": "requester", "last_query_at": WHEN},
        {"slack_user_id": "U0EXAMPLE002", "name": "Sam Archer", "enabled": True,
         "kind": "admin", "last_query_at": None},
    ])
    people = ra.admin_people(claims={"sub": "U0ADMIN"})["people"]
    assert [p["lastQueryAt"] for p in people] == ["2026-10-21T14:12:00+00:00", None]


def test_the_whole_list_is_one_statement_with_one_grouped_read(monkeypatch):
    calls = _wire(monkeypatch, [])
    ra.admin_people(claims={"sub": "U0ADMIN"})
    assert len(calls) == 1
    sql = calls[0]
    assert sql.count("GROUP BY") == 1
    assert ("LEFT JOIN (SELECT requester_slack_id, max(created_at) AS last_query_at "
            "FROM requests WHERE status <> 'draft' GROUP BY requester_slack_id) lq "
            "ON lq.requester_slack_id = p.slack_user_id") in sql


def test_the_shape_of_a_row_only_grows(monkeypatch):
    """Additive: the keys the panel reads today are all still there."""
    _wire(monkeypatch, [{"slack_user_id": "U0EXAMPLE001", "name": None,
                         "enabled": False, "kind": "grant_only"}])
    person = ra.admin_people(claims={"sub": "U0ADMIN"})["people"][0]
    assert set(person) == {"id", "handle", "slackId", "name", "initials", "enabled",
                           "kind", "lastQueryAt"}
    assert person["lastQueryAt"] is None
