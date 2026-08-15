"""Team-grant tier resolution must NOT cross-product tier and
database.

The old bug: `effective_grant_for_user` returned the max tier across every
grant on a target together with the UNION of their databases. A user whose
team had `RW on dbA` and `RO on dbB` then appeared to hold `RW` on the whole
union `{dbA, dbB}` — a silent write-escalation to the read-only database.

`effective_mode_for_database` resolves the tier for one specific database:
a grant contributes its tier only to the databases it actually covers, and
the result is fail-closed when nothing covers the database.
"""
import pytest

from dba_slack_bot import teams


@pytest.fixture
def restricted_user(monkeypatch):
    """A non-admin, non-bypass user with no user-level override — so
    resolution falls through to the team-grant aggregation."""
    monkeypatch.setattr(teams, "_is_unrestricted", lambda uid: False)
    monkeypatch.setattr(teams.db, "fetch_one", lambda *a, **k: None)
    return "U0EXAMPLE01"


def _team_grants(monkeypatch, rows):
    monkeypatch.setattr(teams.db, "fetch_all", lambda *a, **k: rows)


def test_rw_on_one_db_does_not_leak_to_ro_only_db(restricted_user, monkeypatch):
    _team_grants(monkeypatch, [
        {"mode": "rw", "allowed_databases": ["dbA"]},
        {"mode": "ro", "allowed_databases": ["dbB"]},
    ])
    # The write grant covers only dbA; dbB stays read-only (the fix).
    assert teams.effective_mode_for_database(restricted_user, 1, "dbA") == "rw"
    assert teams.effective_mode_for_database(restricted_user, 1, "dbB") == "ro"


def test_uncovered_database_is_fail_closed(restricted_user, monkeypatch):
    _team_grants(monkeypatch, [
        {"mode": "rw", "allowed_databases": ["dbA"]},
    ])
    assert teams.effective_mode_for_database(restricted_user, 1, "dbC") is None


def test_two_grants_on_same_db_take_the_higher_tier(restricted_user, monkeypatch):
    _team_grants(monkeypatch, [
        {"mode": "ro", "allowed_databases": ["dbA"]},
        {"mode": "rw", "allowed_databases": ["dbA"]},
    ])
    assert teams.effective_mode_for_database(restricted_user, 1, "dbA") == "rw"


def test_unrestricted_grant_covers_every_database(restricted_user, monkeypatch):
    # allowed_databases NULL (or empty) = all databases on the target.
    _team_grants(monkeypatch, [
        {"mode": "rw", "allowed_databases": None},
    ])
    assert teams.effective_mode_for_database(restricted_user, 1, "anything") == "rw"
    _team_grants(monkeypatch, [
        {"mode": "rw", "allowed_databases": []},
    ])
    assert teams.effective_mode_for_database(restricted_user, 1, "anything") == "rw"


def test_no_team_grant_is_none(restricted_user, monkeypatch):
    _team_grants(monkeypatch, [])
    assert teams.effective_mode_for_database(restricted_user, 1, "dbA") is None


def test_user_override_wins_and_is_scoped_to_its_databases(monkeypatch):
    monkeypatch.setattr(teams, "_is_unrestricted", lambda uid: False)
    # user_target_grants row present → team grants are never consulted.
    monkeypatch.setattr(teams.db, "fetch_one",
                        lambda *a, **k: {"mode": "rw", "allowed_databases": ["dbA"]})
    monkeypatch.setattr(teams.db, "fetch_all", lambda *a, **k: [
        {"mode": "ddl", "allowed_databases": None},  # would win if consulted
    ])
    assert teams.effective_mode_for_database("U0EXAMPLE01", 1, "dbA") == "rw"
    # A database outside the override's whitelist is denied even though the
    # team grant is unrestricted DDL — the user override is exhaustive.
    assert teams.effective_mode_for_database("U0EXAMPLE01", 1, "dbB") is None


def test_admin_gets_ddl_everywhere(monkeypatch):
    monkeypatch.setattr(teams, "_is_unrestricted", lambda uid: True)
    assert teams.effective_mode_for_database("U0ADMIN0001", 1, "whatever") == "ddl"
