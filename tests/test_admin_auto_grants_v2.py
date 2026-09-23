"""Auto-approve on the admin screen, under the new model: teams, bulk, revoke.

A team's waiver decides at submit but showed nowhere on the Auto-approve
screen -- the list read only the per-person legacy table -- so an admin could
not see, let alone revoke, the waiver that let a whole pod skip review. And
giving one person the same waiver on four targets meant picking the person
four times.
"""
import inspect

import pytest
from fastapi import HTTPException

from queryhub import auto_approve
from queryhub.web import routes_admin as ra

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Example Super"}


class Cur:
    def __init__(self, returning=None):
        self.sql, self.returning = [], list(returning or [])
        self._row = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._row = self.returning.pop(0) if self.returning else {"id": 1}

    def fetchone(self):
        return self._row


class Txn:
    def __init__(self, cur): self.cur = cur
    def __enter__(self): return self.cur
    def __exit__(self, *e): return False


@pytest.fixture
def env(monkeypatch):
    st = {"cur": Cur(), "audit": [], "v2": True, "live": [], "team": {"id": 7, "name": "Team Alpha"}}
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, a: CLAIMS["sub"])
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: st["v2"])
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn(st["cur"]))
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: st["live"])
    monkeypatch.setattr(ra.audit, "log_in", lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((action, details)))
    monkeypatch.setattr(ra, "_target_id_of", lambda c: {"prod-ledger": 53, "prod-orders": 60}.get(c))
    monkeypatch.setattr(ra, "_resolve_team", lambda n: st["team"] if n == "Team Alpha" else None)
    monkeypatch.setattr(auto_approve, "validate_scope", lambda tid, dbn: None)
    return st


def _bulk(**kw):
    kw.setdefault("targets", [{"connectionId": "prod-ledger", "databaseId": "ledger"},
                              {"connectionId": "prod-orders"}])
    return ra.BulkAutoGrantIn(**kw)


# --- bulk --------------------------------------------------------------------

def test_one_person_many_targets_in_one_transaction(env):
    env["cur"].returning = [{"id": 11}, {"id": 12}]
    out = ra.admin_bulk_create_auto_grants(_bulk(subject="U0EXAMPLE002"), claims=CLAIMS)
    assert out == {"applied": 2, "ids": ["11", "12"],
                   "targets": ["prod-ledger/ledger", "prod-orders"]}
    assert all("INSERT INTO auto_approve_grants" in q for q, _ in env["cur"].sql)
    assert [a for a, _ in env["audit"]] == ["auto_approve_granted"] * 2


def test_a_team_waiver_is_an_access_grant_row(env):
    env["cur"].returning = [{"id": 501}, {"id": 502}]
    out = ra.admin_bulk_create_auto_grants(_bulk(subjectType="team", subject="Team Alpha"),
                                           claims=CLAIMS)
    assert out["ids"] == ["ag:501", "ag:502"]
    [q, params] = env["cur"].sql[0]
    assert "INSERT INTO access_grant" in q and "TRUE, FALSE, NOW()" in q
    assert params[0] == 7                                  # the team id


def test_one_refusal_writes_nothing(env):
    with pytest.raises(HTTPException) as e:
        ra.admin_bulk_create_auto_grants(
            _bulk(subject="U0EXAMPLE002",
                  targets=[{"connectionId": "prod-ledger"}, {"connectionId": "nope"}]),
            claims=CLAIMS)
    assert e.value.status_code == 409
    assert [r["target"] for r in e.value.detail["refused"]] == ["nope"]
    assert env["cur"].sql == []


def test_a_waiver_the_team_already_holds_is_refused_not_duplicated(env):
    env["live"] = [{"target_id": 53, "database_name": "ledger"}]
    with pytest.raises(HTTPException) as e:
        ra.admin_bulk_create_auto_grants(_bulk(subjectType="team", subject="Team Alpha"),
                                         claims=CLAIMS)
    assert [r["target"] for r in e.value.detail["refused"]] == ["prod-ledger/ledger"]


def test_a_team_waiver_needs_the_new_model(env):
    env["v2"] = False
    with pytest.raises(HTTPException) as e:
        ra.admin_bulk_create_auto_grants(_bulk(subjectType="team", subject="Team Alpha"),
                                         claims=CLAIMS)
    assert e.value.status_code == 400


def test_duplicate_targets_are_written_once_and_dry_run_writes_nothing(env):
    out = ra.admin_bulk_create_auto_grants(
        _bulk(subject="U0EXAMPLE002", dryRun=True,
              targets=[{"connectionId": "prod-ledger"}, {"connectionId": "prod-ledger"}]),
        claims=CLAIMS)
    assert out == {"applied": 0, "targets": ["prod-ledger"]}
    assert env["cur"].sql == []


def test_the_single_route_and_the_bulk_route_write_a_person_the_same_way():
    assert "_insert_user_waiver(cur, body.user" in inspect.getsource(ra.admin_create_auto_grant)
    assert "_insert_user_waiver(cur, body.subject" in inspect.getsource(ra.admin_bulk_create_auto_grants)


# --- the list ----------------------------------------------------------------

def test_the_list_includes_team_waivers_under_the_new_model():
    src = inspect.getsource(ra.admin_auto_grants)
    assert "if teams_mod.use_v2():" in src and "_v2_only_auto_grants()" in src
    v2 = inspect.getsource(ra._v2_only_auto_grants)
    assert "g.auto_approve AND g.mirrored_from IS NULL" in v2
    assert '"subjectType": "team" if team else "user"' in v2
    assert '"id": f"ag:{r[\'id\']}"' in v2


# --- the revoke --------------------------------------------------------------

def test_an_ag_id_revokes_the_access_grant_row(env):
    env["cur"].returning = [{"team_id": 7, "principal_id": None, "target_id": 53}]
    ra.admin_delete_auto_grant("ag:501", claims=CLAIMS)
    [(q, params)] = env["cur"].sql
    assert q.startswith("UPDATE access_grant SET revoked_at = NOW()")
    assert "mirrored_from IS NULL" in q and params[1] == 501


def test_a_legacy_id_still_deletes_the_legacy_row(env):
    env["cur"].returning = [{"slack_user_id": "U0EXAMPLE002", "target_server_id": 53}]
    ra.admin_delete_auto_grant("41", claims=CLAIMS)
    [(q, _)] = env["cur"].sql
    assert q.startswith("DELETE FROM auto_approve_grants")


def test_a_bad_id_is_a_400(env):
    for bad in ("ag:x", "x"):
        with pytest.raises(HTTPException) as e:
            ra.admin_delete_auto_grant(bad, claims=CLAIMS)
        assert e.value.status_code == 400
