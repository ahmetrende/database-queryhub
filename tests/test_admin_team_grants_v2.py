"""Team grants on the Grants and Teams screens follow the access-model switch.

After the pod cutover every team grant is an `access_grant` row and
`team_target_grants` is empty. The list read only the empty table, so every
team card printed "No targets" while seven teams held 35 grants; the revoke
deleted from the same empty table, so every attempt answered 404 while the
access stayed. Neither failed loudly: an empty table answers "nothing".

The name a team grant carries is part of the contract. `mapGrants` in the web
client keys a team grant on `subjectName`, the Teams list shows
COALESCE(display_name, name), and `_resolve_team` accepts that same string on
the way back in. A grant whose name does not match the list is a grant the
card cannot find.

Routes are called directly with fake claims -- no TestClient, no DB.
"""
import pytest
from fastapi import HTTPException

from queryhub.web import routes_admin as ra

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Example Admin"}


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeTxn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *exc):
        return False


@pytest.fixture
def env(monkeypatch):
    state = {"v2": True, "sql": [], "cur": FakeCursor([]), "audit": []}
    team_row = {"team_id": 7, "subject_name": "Team Alpha", "target_server_id": 53,
                "allowed_databases": ["ledger"], "mode": "rw",
                "granted_at": None, "expires_at": None}

    def fetch_all(sql, params=None):
        state["sql"].append(sql)
        if "FROM access_grant" in sql or "FROM team_target_grants" in sql:
            return [dict(team_row)]
        return []                       # the user-grant query

    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, area: CLAIMS["sub"])
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: state["v2"])
    monkeypatch.setattr(ra.db, "fetch_all", fetch_all)
    monkeypatch.setattr(ra.db, "transaction", lambda: FakeTxn(state["cur"]))
    monkeypatch.setattr(ra, "_alias_of", lambda tid: {53: "prod-ledger"}.get(tid))
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        state["audit"].append((action, details)))
    return state


# --- the list ----------------------------------------------------------------

def test_team_grants_are_read_from_the_live_model(env):
    ra.admin_grants(claims=CLAIMS)
    team_sql = [s for s in env["sql"] if "team" in s.lower() and "access_grant" in s]
    assert team_sql, "under v2 the team grants must come from access_grant"
    assert not any("team_target_grants" in s for s in env["sql"])


def test_a_team_grant_carries_the_name_the_teams_list_shows(env):
    [g] = [g for g in ra.admin_grants(claims=CLAIMS)["grants"]
           if g["subjectType"] == "team"]
    assert g["subjectName"] == "Team Alpha"
    assert g["id"] == "t:7:53"
    assert g["connectionId"] == "prod-ledger"
    assert g["databases"] == ["ledger"]
    assert g["tier"] == "RW"


def test_the_name_expression_matches_the_teams_list(env):
    """The four places a team is named must agree, or the card finds nothing."""
    import inspect
    src = inspect.getsource(ra.admin_grants)
    assert "COALESCE(t.display_name, t.name) AS subject_name" in src
    assert "COALESCE(display_name, name) AS name" in inspect.getsource(ra._resolve_team)
    assert 't["display_name"] or t["name"]' in inspect.getsource(ra._teams_payload)


def test_waivers_and_fleet_wide_rows_are_not_listed_as_grants(env):
    ra.admin_grants(claims=CLAIMS)
    [sql] = [s for s in env["sql"] if "FROM access_grant" in s]
    assert "NOT g.auto_approve" in sql
    assert "NOT g.all_targets" in sql
    assert "g.revoked_at IS NULL AND NOT g.is_deleted" in sql


def test_the_tier_is_ranked_not_sorted_as_text(env):
    """max('ddl', 'rw') on text is 'rw' -- the wrong answer for the top tier."""
    ra.admin_grants(claims=CLAIMS)
    [sql] = [s for s in env["sql"] if "FROM access_grant" in s]
    assert "ORDER BY tr.rank DESC" in sql
    assert "max(g.tier)" not in sql


def test_the_legacy_branch_is_kept_for_the_old_model(env):
    env["v2"] = False
    ra.admin_grants(claims=CLAIMS)
    assert any("FROM team_target_grants" in s for s in env["sql"])
    assert not any("FROM access_grant" in s for s in env["sql"])


# --- the revoke --------------------------------------------------------------

def test_revoking_a_team_grant_revokes_the_live_rows(env):
    env["cur"].rows = [{"id": 101}, {"id": 102}]
    ra.admin_delete_grant("t:7:53", claims=CLAIMS)
    [(sql, params)] = env["cur"].sql
    assert sql.startswith("UPDATE access_grant SET revoked_at = NOW()")
    assert "NOT auto_approve" in sql
    assert "team_target_grants" not in sql
    assert params == (CLAIMS["sub"], 7, 53)
    assert env["audit"] == [("team_grant_removed",
                             {"team_id": 7, "target_id": 53, "rows": 2})]


def test_revoke_is_soft_and_records_who(env):
    env["cur"].rows = [{"id": 101}]
    ra.admin_delete_grant("t:7:53", claims=CLAIMS)
    [(sql, _)] = env["cur"].sql
    assert "DELETE" not in sql
    assert "revoked_by = (SELECT p.id FROM principal p" in sql


def test_revoking_nothing_is_a_404_not_a_silent_success(env):
    env["cur"].rows = []
    with pytest.raises(HTTPException) as e:
        ra.admin_delete_grant("t:7:53", claims=CLAIMS)
    assert e.value.status_code == 404
    assert env["audit"] == []


def test_the_legacy_revoke_is_kept_for_the_old_model(env):
    env["v2"] = False
    env["cur"].rows = [{"team_id": 7}]
    ra.admin_delete_grant("t:7:53", claims=CLAIMS)
    [(sql, _)] = env["cur"].sql
    assert sql.startswith("DELETE FROM team_target_grants")
