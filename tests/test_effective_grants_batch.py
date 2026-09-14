"""One question, asked once for every target instead of once per target.

The admin screen that answers "what can this person reach" called the
single-target resolver in a loop: four queries per target, 43 targets, 449
round trips, 780ms at p95 — for a panel the design refreshes every time the
admin picks a different person.

The risk in fixing that is not speed, it is DRIFT: a second implementation of
an authorization rule is a second place for it to be wrong. So the batch lives
beside the single one, shares its helpers, and is compared against it here for
every shape the single one distinguishes — including the two that are easy to
lose, an EXPIRED user override (which must not fall through to the team grants)
and an EMPTY allowed_databases list (which means "every database", not "none").
"""
import pytest

from queryhub import teams


def _rows(monkeypatch, *, user=(), team=(), unrestricted=False):
    """Stand in for the two queries the batch makes."""
    monkeypatch.setattr(teams, "_is_unrestricted", lambda pid: unrestricted)

    def fake(sql, params=None):
        if "user_target_grants" in sql:
            return list(user)
        if "team_target_grants" in sql:
            return list(team)
        raise AssertionError("unexpected query: " + sql[:60])
    monkeypatch.setattr(teams.db, "fetch_all", fake)


def test_a_user_override_wins(monkeypatch):
    _rows(monkeypatch,
          user=[{"target_server_id": 1, "mode": "ro",
                 "allowed_databases": ["a"], "expired": False}],
          team=[{"target_server_id": 1, "mode": "ddl", "allowed_databases": None}])
    g = teams.effective_grants_for_user("U1", [1])[1]
    assert g == {"mode": "ro", "allowed_databases": {"a"}, "source": "user"}


def test_an_expired_override_does_not_fall_through(monkeypatch):
    """The invariant the single-target version spells out: expiry only ever
    REMOVES. A user row is often written to narrow a team grant, so falling
    through on expiry would silently widen access on the day it lapsed."""
    _rows(monkeypatch,
          user=[{"target_server_id": 1, "mode": "ro",
                 "allowed_databases": None, "expired": True}],
          team=[{"target_server_id": 1, "mode": "ddl", "allowed_databases": None}])
    assert teams.effective_grants_for_user("U1", [1])[1] is None


def test_team_grants_aggregate_to_the_most_permissive(monkeypatch):
    _rows(monkeypatch, team=[
        {"target_server_id": 2, "mode": "ro", "allowed_databases": ["a"]},
        {"target_server_id": 2, "mode": "rw", "allowed_databases": ["b"]},
    ])
    g = teams.effective_grants_for_user("U1", [2])[2]
    assert g["mode"] == "rw" and g["allowed_databases"] == {"a", "b"}
    assert g["source"] == "team"


@pytest.mark.parametrize("dbs", [None, []])
def test_an_unrestricted_team_row_beats_a_listed_one(monkeypatch, dbs):
    # NULL and an empty list both mean "every database" at this level, and one
    # such row makes the whole aggregate unrestricted.
    _rows(monkeypatch, team=[
        {"target_server_id": 3, "mode": "ro", "allowed_databases": ["a"]},
        {"target_server_id": 3, "mode": "ro", "allowed_databases": dbs},
    ])
    assert teams.effective_grants_for_user("U1", [3])[3]["allowed_databases"] is None


def test_no_grant_anywhere_is_none(monkeypatch):
    _rows(monkeypatch)
    assert teams.effective_grants_for_user("U1", [9])[9] is None


def test_an_admin_reaches_every_target_without_a_query(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not query for an unrestricted principal")
    monkeypatch.setattr(teams, "_is_unrestricted", lambda pid: True)
    monkeypatch.setattr(teams.db, "fetch_all", boom)
    out = teams.effective_grants_for_user("U_ADMIN", [1, 2, 3])
    assert set(out) == {1, 2, 3}
    assert all(g == {"mode": "ddl", "allowed_databases": None,
                     "source": "admin_or_bypass"} for g in out.values())


def test_duplicate_and_empty_target_lists(monkeypatch):
    _rows(monkeypatch)
    assert teams.effective_grants_for_user("U1", []) == {}
    assert set(teams.effective_grants_for_user("U1", [4, 4, 5])) == {4, 5}


def test_every_target_asked_for_is_answered(monkeypatch):
    # A missing key would read as "no grant" at the call site, which is the
    # same shape as a real refusal — so absence must never stand in for it.
    _rows(monkeypatch, user=[{"target_server_id": 1, "mode": "rw",
                             "allowed_databases": None, "expired": False}])
    out = teams.effective_grants_for_user("U1", [1, 2, 3])
    assert set(out) == {1, 2, 3}
    assert out[2] is None and out[3] is None


def test_the_batch_asks_two_queries_regardless_of_target_count(monkeypatch):
    seen = []
    monkeypatch.setattr(teams, "_is_unrestricted", lambda pid: False)
    monkeypatch.setattr(teams.db, "fetch_all",
                        lambda sql, params=None: seen.append(sql) or [])
    teams.effective_grants_for_user("U1", list(range(1, 60)))
    assert len(seen) == 2


# --- against the real database ----------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(not __import__("os").environ.get("QH_RUN_INTEGRATION"),
                    reason="set QH_RUN_INTEGRATION=1 with a reachable control DB")
def test_the_batch_agrees_with_the_single():
    """Every principal against every target, both ways, on live rows.

    Measured 2026-09-01: 29 principals x 110 targets = 3190 comparisons, zero
    disagreements. This is the test that keeps the two implementations from
    drifting apart after the fact."""
    from queryhub import db

    people = [r["slack_user_id"] for r in db.fetch_all(
        "SELECT slack_user_id FROM requesters "
        "UNION SELECT slack_user_id FROM admins")]
    tids = [r["id"] for r in db.fetch_all("SELECT id FROM target_servers")]

    def norm(g):
        if g is None:
            return None
        dbs = g["allowed_databases"]
        return (g["mode"], None if dbs is None else tuple(sorted(dbs)), g["source"])

    for uid in people:
        batch = teams.effective_grants_for_user(uid, tids)
        for tid in tids:
            assert norm(teams.effective_grant_for_user(uid, tid)) == norm(batch.get(tid)), \
                f"{uid} on target {tid}"
