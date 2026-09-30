"""POST /admin/grants with auto-approve: one transaction, both kinds of subject.

A person's waiver goes through `grants.grant_many` (tested in
test_grant_with_auto_approve.py); what this file pins is the route around it:
the tier check before anything is written, the outcome the response carries,
and the TEAM branch, which writes its waiver into `access_grant` itself --
a team has no legacy table to be mirrored from.

Routes are called directly with fake claims -- no TestClient, no DB.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub.web import routes_admin as ra

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Example Admin"}
TEAM = {"id": 7, "name": "Team Alpha"}
PAST = datetime.now(timezone.utc) - timedelta(days=1)


class Cur:
    def __init__(self):
        self.sql = []
        self._next = 900

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        self._next += 1
        return {"id": self._next}


class Txn:
    def __init__(self, st):
        self.st = st

    def __enter__(self):
        self.st.txns += 1
        return self.st.cur

    def __exit__(self, *exc):
        return False


@pytest.fixture
def env(monkeypatch):
    st = SimpleNamespace(cur=Cur(), txns=0, v2=True, team_waivers=[], audit=[], calls=[],
                         outcome=None)

    def grant_many(**kw):
        st.calls.append(kw)
        return [{"grantee_id": gid, "mode": kw["mode"], "databases": kw["databases"],
                 "whitelisted_now": False, "auto_approve": st.outcome}
                for gid, _profile in kw["grantees"]]

    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, area: CLAIMS["sub"])
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: st.v2)
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn(st))
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: st.team_waivers)
    monkeypatch.setattr(ra.db, "fetch_one", lambda sql, params=None: {"name": "Jordan Ray"})
    monkeypatch.setattr(ra.audit, "log_in", lambda cur, rid, uid, name, action, details=None:
                        st.audit.append((action, details)))
    monkeypatch.setattr(ra, "_target_id_of", lambda c: {"prod-ledger": 53}.get(c))
    monkeypatch.setattr(ra, "_resolve_team", lambda n: TEAM if n == "Team Alpha" else None)
    monkeypatch.setattr(ra, "_slack_profile", lambda uid: {})
    monkeypatch.setattr(ra.grants, "control_plane_target_ids", lambda: {99})
    monkeypatch.setattr(ra.grants, "grant_many", grant_many)
    monkeypatch.setattr("queryhub.targets.get", lambda tid: SimpleNamespace(alias="prod-ledger"))
    return st


def _post(**kw):
    body = {"subjectType": "user", "subject": "U0EXAMPLE002", "connectionId": "prod-ledger",
            "tier": "rw"}
    body.update(kw)
    return ra.admin_create_grant(ra.GrantIn(**body), claims=CLAIMS)


# --- a person ----------------------------------------------------------------

def test_the_waiver_rides_with_the_grant_at_ro_by_default(env):
    env.outcome = {"tier": "ro", "written": [{"id": "41", "database": None}], "skipped": []}
    out = _post(autoApprove=True)
    [call] = env.calls
    assert call["auto_approve_tier"] == "ro"
    assert out["autoApprove"] == {"tier": "RO", "written": 1, "ids": ["41"], "skipped": []}


def test_a_skipped_waiver_is_named_in_the_response(env):
    env.outcome = {"tier": "ro", "written": [], "skipped": [
        {"database": None, "covered_by": "12", "by": "auto-approve #12 (...)",
         "reason": "already covered by auto-approve #12 (...)"}]}
    out = _post(autoApprove=True, subjects=["U0EXAMPLE002", "U0EXAMPLE003"])
    a = out["autoApprove"]
    assert a["written"] == 0
    assert [(s["subject"], s["coveredBy"]) for s in a["skipped"]] == [
        ("U0EXAMPLE002", "12"), ("U0EXAMPLE003", "12")]
    assert a["skipped"][0]["reason"].startswith("already covered by")


def test_without_the_box_no_waiver_is_asked_for(env):
    out = _post(autoApproveTier="rw")            # a tier alone asks for nothing
    assert env.calls[0]["auto_approve_tier"] is None
    assert out["autoApprove"] is None


@pytest.mark.parametrize("tier,auto", [("ro", "rw"), ("ddl", "ddl"), ("rw", "nope")])
def test_a_waiver_tier_the_grant_cannot_carry_is_a_400_before_any_write(env, tier, auto):
    with pytest.raises(HTTPException) as e:
        _post(tier=tier, autoApprove=True, autoApproveTier=auto)
    assert e.value.status_code == 400
    assert env.calls == [] and env.txns == 0


@pytest.mark.parametrize("dbs,want", [(["*"], None), (["ledger", "*"], None), ([], None),
                                      (["ledger", "audit", "ledger"], ["ledger", "audit"])])
def test_all_databases_is_every_database_not_one_named_star(env, dbs, want):
    _post(databases=dbs)
    assert env.calls[0]["databases"] == want


# --- a team ------------------------------------------------------------------

def _team(**kw):
    return _post(subjectType="team", subject="Team Alpha", **kw)


def test_a_team_waiver_is_written_in_the_grants_own_transaction(env):
    out = _team(autoApprove=True, databases=["ledger"], expiresAt="2099-01-01T00:00:00Z")
    assert env.txns == 1
    inserts = [(q, p) for q, p in env.cur.sql if q.startswith("INSERT INTO access_grant")]
    assert len(inserts) == 2
    (grant_q, grant_p), (waiver_q, waiver_p) = inserts
    assert "auto_approve" not in grant_q                        # the grant row
    assert "TRUE, FALSE, NOW()" in waiver_q                     # auto_approve, no merge
    assert "mirrored_from" not in waiver_q                      # left NULL: nobody's copy
    team_id, tid, dbn, all_dbs, tier, until, reason, uid = waiver_p
    assert (team_id, tid, dbn, all_dbs, tier) == (7, 53, "ledger", False, "ro")
    assert until == datetime(2099, 1, 1, tzinfo=timezone.utc)   # the grant's own expiry
    assert reason == "auto-approve with the grant" and uid == CLAIMS["sub"]
    assert out["autoApprove"]["written"] == 1 and out["autoApprove"]["ids"][0].startswith("ag:")
    assert ("auto_approve_granted", {"subject_type": "team", "team_id": 7, "target_id": 53,
            "database": "ledger", "tier": "ro", "grant_id": out["autoApprove"]["ids"][0],
            "with_grant": True, "expires_at": "2099-01-01T00:00:00+00:00"}) in env.audit


def test_a_team_waiver_the_team_already_holds_is_skipped(env):
    env.team_waivers = [{"id": 458, "tier": "ro", "target_id": 53, "database_name": None,
                         "valid_from": PAST, "valid_until": None}]
    out = _team(autoApprove=True, databases=["ledger"])
    assert [q for q, _ in env.cur.sql if "auto_approve" in q and "INSERT" in q] == []
    [s] = out["autoApprove"]["skipped"]
    assert s["coveredBy"] == "ag:458" and s["subject"] == "Team Alpha"
    assert "all databases on prod-ledger" in s["reason"]
    # The grant's audit row is the trail's record of what was asked and why none was written.
    [added] = [d for a, d in env.audit if a == "team_grant_added"]
    assert added["auto_approve"] == "ro" and added["auto_approve_covered_by"] == ["ag:458"]


def test_a_same_scope_team_waiver_that_ends_sooner_is_superseded_not_duplicated(env):
    """`access_grant_live_uq` holds one live row per scope and tier, ended or
    not, so writing beside it would fail; the old row is revoked first."""
    env.team_waivers = [{"id": 459, "tier": "ro", "target_id": 53, "database_name": "ledger",
                         "valid_from": PAST, "valid_until": PAST + timedelta(hours=2)}]
    _team(autoApprove=True, databases=["ledger"])
    waiver_sql = [(q, p) for q, p in env.cur.sql if "auto_approve AND tier" in q
                  or q.startswith("INSERT INTO access_grant (team_id")]
    (revoke_q, revoke_p), (insert_q, _p) = waiver_sql
    assert revoke_q.startswith("UPDATE access_grant SET revoked_at = NOW()")
    assert revoke_p == (CLAIMS["sub"], 7, 53, "ro", "ledger")
    assert "INSERT INTO access_grant" in insert_q


def test_a_team_waiver_needs_the_new_model(env):
    env.v2 = False
    with pytest.raises(HTTPException) as e:
        _team(autoApprove=True)
    assert e.value.status_code == 400
    assert env.cur.sql == [] and env.txns == 0


def test_a_team_grant_without_the_box_writes_no_waiver(env):
    out = _team()
    assert not [q for q, _ in env.cur.sql if "TRUE, FALSE, NOW()" in q]
    assert out["autoApprove"] is None
