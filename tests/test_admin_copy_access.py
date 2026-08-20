"""Copying a colleague's access.

Onboarding is nearly always "give them what X has". Done by hand it means
reading X's grants and remembering that most of them usually arrive through a
team — which is the step that gets missed, and the person starts work missing
half their targets.
"""
from contextlib import contextmanager

import pytest

from queryhub.web import routes_admin as ra


class _Cur:
    """Just enough cursor: canned SELECTs, recorded INSERTs."""
    def __init__(self, user_grants, team_grants, joined=()):
        self._user, self._team, self._joined = user_grants, team_grants, list(joined)
        self._last = None
        self.rowcount = 0
        self.inserted = []          # (target_id, mode, allowed_databases)
        self.audit = {}

    def execute(self, sql, params=()):
        self._last = sql
        if "FROM user_target_grants" in sql and "SELECT" in sql.split("FROM")[0]:
            self._rows = self._user
        elif "team_target_grants" in sql and "SELECT" in sql.split("FROM")[0]:
            self._rows = self._team
        elif "INSERT INTO team_members" in sql:
            self._rows = [{"team_id": t} for t in self._joined]
        elif "INSERT INTO user_target_grants" in sql:
            self.inserted.append((params[1], params[3], params[2]))
            self.rowcount = 1
            self._rows = []
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


@pytest.fixture
def wired(monkeypatch):
    def _wire(user_grants, team_grants, joined=(), control_plane=frozenset()):
        cur = _Cur(user_grants, team_grants, joined)

        @contextmanager
        def fake_txn():
            yield cur

        monkeypatch.setattr(ra.admin, "require_admin", lambda c, s: "UADMIN")
        monkeypatch.setattr(ra.db, "transaction", fake_txn)
        monkeypatch.setattr(ra.grants, "control_plane_target_ids",
                            lambda: set(control_plane))
        monkeypatch.setattr(ra.audit, "log_in",
                            lambda c, r, u, n, a, d: cur.audit.update(d))
        return cur
    return _wire


def _g(tid, mode="rw", dbs=None):
    return {"target_server_id": tid, "mode": mode, "allowed_databases": dbs}


def test_include_teams_joins_teams_and_copies_only_personal_grants(wired):
    cur = wired(user_grants=[_g(3)], team_grants=[_g(4), _g(8)], joined=[2])
    out = ra.admin_copy_access(
        "U06NEWUSER1", ra.CopyAccessIn(source="U07SOURCE01", includeTeams=True), {})
    assert out["teamsJoined"] == 1
    # Team-derived targets arrive through membership, so they are not duplicated
    # as per-user rows.
    assert [t for t, _, _ in cur.inserted] == [3]


def test_without_teams_the_team_targets_become_explicit_grants(wired):
    """The failure this exists to prevent: dropping membership drops the access
    it was carrying, which is usually most of it."""
    cur = wired(user_grants=[_g(3)], team_grants=[_g(4), _g(8)])
    out = ra.admin_copy_access(
        "U06NEWUSER1", ra.CopyAccessIn(source="U07SOURCE01", includeTeams=False), {})
    assert out["teamsJoined"] == 0
    assert sorted(t for t, _, _ in cur.inserted) == [3, 4, 8]


def test_a_personal_grant_wins_over_the_team_one_for_the_same_target(wired):
    """A user row is usually written to NARROW what a team allows; taking the
    team's copy instead would widen access while claiming to copy it."""
    cur = wired(user_grants=[_g(4, mode="ro", dbs=["only_this"])],
                team_grants=[_g(4, mode="ddl")])
    ra.admin_copy_access("U06NEWUSER1",
                         ra.CopyAccessIn(source="U07SOURCE01", includeTeams=False), {})
    assert cur.inserted == [(4, "ro", ["only_this"])]


def test_tier_override_applies_to_every_copied_grant(wired):
    cur = wired(user_grants=[_g(3, mode="ddl")], team_grants=[_g(4, mode="ddl")])
    ra.admin_copy_access(
        "U06NEWUSER1",
        ra.CopyAccessIn(source="U07SOURCE01", includeTeams=False, tier="ro"), {})
    assert {m for _, m, _ in cur.inserted} == {"ro"}


def test_the_control_plane_is_never_copied(wired):
    cur = wired(user_grants=[_g(1), _g(3)], team_grants=[], control_plane={1})
    ra.admin_copy_access(
        "U06NEWUSER1", ra.CopyAccessIn(source="U07SOURCE01", includeTeams=False), {})
    assert [t for t, _, _ in cur.inserted] == [3]
    assert cur.audit["skipped_control_plane"] == [1]


def test_copying_onto_yourself_is_refused(wired):
    wired([], [])
    with pytest.raises(Exception):
        ra.admin_copy_access("U07SOURCE01", ra.CopyAccessIn(source="U07SOURCE01"), {})


def test_a_bad_tier_is_refused(wired):
    wired([], [])
    with pytest.raises(Exception):
        ra.admin_copy_access("U06NEWUSER1",
                             ra.CopyAccessIn(source="U07SOURCE01", tier="admin"), {})
