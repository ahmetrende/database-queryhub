"""When access to a database ends, shown per database in GET /api/connections."""
from datetime import datetime, timedelta, timezone

from queryhub import teams
from queryhub.web import routes_data

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
SOON = NOW + timedelta(days=2)
LATER = NOW + timedelta(days=9)
PAST = NOW - timedelta(days=1)


def _g(mode, ends_at, dbs=None):
    return {"mode": mode, "allowed_databases": dbs, "ends_at": ends_at}


def _end(rows, database="app"):
    return teams.grant_end_for(rows, database, now=NOW)


def test_one_dated_grant_gives_its_end():
    assert _end([_g("ro", SOON)]) == SOON


def test_team_and_own_equal_tier_the_last_end_wins():
    assert _end([_g("rw", SOON), _g("rw", LATER)]) == LATER


def test_a_permanent_top_tier_grant_makes_it_none():
    assert _end([_g("rw", SOON), _g("rw", None)]) is None


def test_an_ended_grant_is_skipped():
    assert _end([_g("rw", PAST), _g("rw", SOON)]) == SOON


def test_only_ended_grants_give_none():
    assert _end([_g("rw", PAST)]) is None


def test_no_grants_gives_none():
    assert _end([]) is None
    assert _end(None) is None


def test_a_lower_tier_that_ends_later_does_not_extend_the_top_tier():
    assert _end([_g("rw", SOON), _g("ro", LATER)]) == SOON


def test_a_permanent_lower_tier_does_not_make_it_permanent():
    assert _end([_g("rw", SOON), _g("ro", None)]) == SOON


def test_an_ended_top_tier_leaves_the_lower_tier():
    assert _end([_g("rw", PAST), _g("ro", LATER)]) == LATER


def test_a_grant_for_another_database_is_ignored():
    rows = [_g("rw", LATER, {"other"}), _g("ro", SOON, {"app"})]
    assert _end(rows, "app") == SOON


def test_the_iso_form_is_utc_with_z():
    assert routes_data._iso_utc(SOON) == "2026-10-26T12:00:00Z"
    assert routes_data._iso_utc(None) is None


# --- the batch resolver carries the ends --------------------------------------

def test_the_batch_carries_grant_rows(monkeypatch):
    monkeypatch.setattr(teams, "_is_unrestricted", lambda pid: False)
    monkeypatch.setattr(teams, "use_v2", lambda: False)

    def fake(sql, params=None):
        if "user_target_grants" in sql:
            return []
        assert "g.expires_at" in sql
        return [{"target_server_id": 1, "mode": "rw",
                 "allowed_databases": ["app"], "expires_at": SOON},
                {"target_server_id": 1, "mode": "rw",
                 "allowed_databases": None, "expires_at": LATER}]
    monkeypatch.setattr(teams.db, "fetch_all", fake)
    g = teams.effective_grants_for_user("U0EXAMPLE001", [1])[1]
    assert teams.grant_end_for(g["grant_rows"], "app", now=NOW) == LATER
    assert teams.grant_end_for(g["grant_rows"], "zzz", now=NOW) == LATER


def test_an_admin_bypass_has_no_grant_rows(monkeypatch):
    monkeypatch.setattr(teams, "_is_unrestricted", lambda pid: True)
    monkeypatch.setattr(teams, "use_v2", lambda: False)
    monkeypatch.setattr(teams.access, "super_admin_only_ids", lambda ids: set())
    g = teams.effective_grants_for_user("U0EXAMPLE001", [1])[1]
    assert "grant_rows" not in g
    assert teams.grant_end_for(g.get("grant_rows"), "app", now=NOW) is None
