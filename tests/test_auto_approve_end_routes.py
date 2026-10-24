"""An auto-approve rule made in the app needs an end, and is RO or RW.

Every path that writes a rule is tested here, for each refusal and for the
accepted case:

  1. POST /admin/auto-grants
  2. POST /admin/auto-grants/bulk (a person, a team, and a dry run)
  3. POST /admin/grants with autoApprove (a person and a team)
  4. POST /admin/grants/fleet with autoApprove
  5. POST /admin/people/{id}/copy-access with includeAutoApprove
  6. the approval of a window request (`auto_approve_requests.decide_window`)
  7. the two insert helpers, called directly

and the setting itself: GET /admin/auto-grants says `maxDays`, and the System
configuration save refuses a value that would switch the limit off.

Each refusal asserts that nothing was written, so a guard that is removed makes
a test fail. The guard against a NEW writer is tests/test_auto_approve_end_guard.py.
Routes are called directly with fake claims. No TestClient, no DB.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from queryhub import auto_approve, auto_approve_requests as aar
from queryhub import config as cfg
from queryhub.web import routes_admin as ra

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Example Admin"}
TEAM = {"id": 7, "name": "Team Alpha"}
NOW = datetime.now(timezone.utc)
SOON = (NOW + timedelta(days=30)).isoformat()
EDGE = (NOW + timedelta(days=89)).isoformat()
TOO_FAR = (NOW + timedelta(days=200)).isoformat()
PAST = (NOW - timedelta(days=1)).isoformat()
TIER_MESSAGE = "An auto-approve rule can be RO or RW. Schema changes always need a DBA."


class Cur:
    """Records every statement. `returning` feeds fetchone, then {"id": n}."""

    def __init__(self, returning=None):
        self.sql, self.returning, self._next = [], list(returning or []), 700

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        if self.returning:
            return self.returning.pop(0)
        self._next += 1
        return {"id": self._next}

    def fetchall(self):
        return []


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
    st = SimpleNamespace(cur=Cur(), txns=0, audit=[], calls=[], v2=True)

    def grant_many(**kw):
        st.calls.append(kw)
        return [{"grantee_id": gid, "mode": kw["mode"], "databases": kw["databases"],
                 "whitelisted_now": False, "auto_approve": None}
                for gid, _profile in kw["grantees"]]

    monkeypatch.setattr(ra.admin, "require_admin", lambda c, a: CLAIMS["sub"])
    monkeypatch.setattr(ra.admins, "is_super_admin", lambda uid: True)
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: st.v2)
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn(st))
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: [])
    monkeypatch.setattr(ra.db, "fetch_one", lambda sql, params=None: {"name": "Jordan Ray"})
    monkeypatch.setattr(ra.audit, "log_in", lambda cur, rid, uid, name, action, details=None:
                        st.audit.append((action, details)))
    monkeypatch.setattr(ra, "_target_id_of", lambda c: {"prod-ledger": 53, "prod-orders": 60}.get(c))
    monkeypatch.setattr(ra, "_resolve_team", lambda n: TEAM if n == "Team Alpha" else None)
    monkeypatch.setattr(ra, "_slack_profile", lambda uid: {})
    monkeypatch.setattr(auto_approve, "validate_scope", lambda tid, dbn: None)
    monkeypatch.setattr(ra, "_person_reach", lambda sid, scopes: {s: "rw" for s in scopes})
    monkeypatch.setattr(ra, "_team_reach", lambda tid, scopes: {s: "rw" for s in scopes})
    monkeypatch.setattr(ra.grants, "control_plane_target_ids", lambda: {99})
    monkeypatch.setattr(ra.grants, "grant_many", grant_many)
    monkeypatch.setattr("queryhub.targets.get", lambda tid: SimpleNamespace(alias="prod-ledger"))
    return st


def refused(call, code, status=400):
    with pytest.raises(HTTPException) as e:
        call()
    assert e.value.status_code == status
    assert e.value.detail["code"] == code, e.value.detail
    return e.value.detail


def _setting(monkeypatch, days):
    real = cfg.get_setting
    monkeypatch.setattr(cfg, "get_setting",
                        lambda k, d=None: str(days) if k == "auto_approve_max_days" else real(k, d))


# --- 1. POST /admin/auto-grants ----------------------------------------------

def _single(**kw):
    body = {"user": "U0EXAMPLE002", "connectionId": "prod-ledger", "tier": "ro"}
    body.update(kw)
    return ra.admin_create_auto_grant(ra.AutoGrantIn(**body), claims=CLAIMS)


def test_the_single_route_refuses_a_rule_with_no_end(env):
    d = refused(lambda: _single(), "end_required")
    assert d["message"] == "An auto-approve rule needs an end date, at most 90 days from now."
    refused(lambda: _single(expiresInMinutes=0), "end_required")
    refused(lambda: _single(expiresAt=""), "end_required")
    assert env.cur.sql == [] and env.txns == 0 and env.audit == []


def test_the_single_route_refuses_an_end_beyond_the_limit_or_in_the_past(env):
    refused(lambda: _single(expiresAt=TOO_FAR), "end_too_far")
    refused(lambda: _single(expiresInMinutes=91 * 1440), "end_too_far")
    refused(lambda: _single(expiresAt=PAST), "end_in_past")
    refused(lambda: _single(expiresAt="soon"), "bad_request")
    assert env.cur.sql == [] and env.txns == 0


def test_the_single_route_reads_the_limit_from_the_setting(env, monkeypatch):
    _setting(monkeypatch, 7)
    d = refused(lambda: _single(expiresAt=SOON), "end_too_far")
    assert "at most 7 days" in d["message"]
    assert env.cur.sql == []
    assert _single(expiresInMinutes=60)["tier"] == "RO"


@pytest.mark.parametrize("kw", [{"expiresAt": SOON}, {"expiresAt": EDGE},
                                {"expiresInMinutes": 60}, {"expiresInMinutes": 90 * 1440}])
def test_the_single_route_writes_a_rule_with_an_end_within_the_limit(env, kw):
    out = _single(**kw)
    assert out["tier"] == "RO"
    [(q, p)] = env.cur.sql
    assert q.startswith("INSERT INTO auto_approve_grants")
    assert [a for a, _ in env.audit] == ["auto_approve_granted"]


def test_the_single_route_refuses_ddl_and_writes_rw(env):
    d = refused(lambda: _single(tier="ddl", expiresAt=SOON), "tier_not_allowed")
    assert d["message"] == TIER_MESSAGE
    refused(lambda: _single(tier="DDL", expiresInMinutes=60), "tier_not_allowed")
    assert env.cur.sql == [] and env.txns == 0
    assert _single(tier="rw", expiresAt=SOON)["tier"] == "RW"


# --- 2. POST /admin/auto-grants/bulk -----------------------------------------

def _bulk(**kw):
    body = {"subject": "U0EXAMPLE002", "targets": [{"connectionId": "prod-ledger"},
                                                   {"connectionId": "prod-orders"}]}
    body.update(kw)
    return ra.admin_bulk_create_auto_grants(ra.BulkAutoGrantIn(**body), claims=CLAIMS)


def test_the_bulk_route_refuses_a_rule_with_no_end(env):
    refused(lambda: _bulk(), "end_required")
    refused(lambda: _bulk(subjectType="team", subject="Team Alpha"), "end_required")
    assert env.cur.sql == [] and env.txns == 0


def test_the_bulk_route_checks_the_end_before_the_dry_run(env):
    """A preview refuses what a real save would refuse."""
    refused(lambda: _bulk(dryRun=True), "end_required")
    refused(lambda: _bulk(dryRun=True, expiresAt=TOO_FAR), "end_too_far")
    assert _bulk(dryRun=True, expiresAt=SOON) == {"applied": 0, "targets": ["prod-ledger", "prod-orders"]}
    assert env.cur.sql == []


@pytest.mark.parametrize("kw,code", [({"expiresAt": TOO_FAR}, "end_too_far"),
                                     ({"expiresAt": PAST}, "end_in_past"),
                                     ({"expiresInMinutes": 10 ** 9}, "end_too_far")])
def test_the_bulk_route_refuses_an_end_that_is_not_allowed(env, kw, code):
    refused(lambda: _bulk(**kw), code)
    assert env.cur.sql == []


def test_the_bulk_route_refuses_ddl_for_a_person_and_a_team(env):
    refused(lambda: _bulk(tier="ddl", expiresAt=SOON), "tier_not_allowed")
    refused(lambda: _bulk(tier="ddl", subjectType="team", subject="Team Alpha", expiresAt=SOON),
            "tier_not_allowed")
    refused(lambda: _bulk(tier="ddl", dryRun=True, expiresAt=SOON), "tier_not_allowed")
    assert env.cur.sql == []


def test_the_bulk_route_writes_a_person_and_a_team_with_an_end(env):
    out = _bulk(expiresAt=SOON)
    assert out["applied"] == 2
    assert all(q.startswith("INSERT INTO auto_approve_grants") for q, _ in env.cur.sql)
    env.cur.sql.clear()
    out = _bulk(subjectType="team", subject="Team Alpha", expiresInMinutes=120)
    assert all(q.startswith("INSERT INTO access_grant") for q, _ in env.cur.sql)
    assert out["ids"][0].startswith("ag:")


# --- 3. POST /admin/grants with autoApprove ----------------------------------

def _grant(**kw):
    body = {"subjectType": "user", "subject": "U0EXAMPLE002", "connectionId": "prod-ledger",
            "tier": "rw"}
    body.update(kw)
    return ra.admin_create_grant(ra.GrantIn(**body), claims=CLAIMS)


def _team(**kw):
    return _grant(subjectType="team", subject="Team Alpha", **kw)


def test_a_grant_with_auto_approve_and_no_end_is_refused_for_a_person(env):
    refused(lambda: _grant(autoApprove=True), "end_required")
    assert env.calls == [] and env.txns == 0


def test_a_grant_with_auto_approve_and_no_end_is_refused_for_a_team(env):
    refused(lambda: _team(autoApprove=True), "end_required")
    assert env.cur.sql == [] and env.txns == 0


@pytest.mark.parametrize("make", [_grant, _team])
def test_a_grant_with_auto_approve_and_too_long_an_end_is_refused(env, make):
    refused(lambda: make(autoApprove=True, expiresAt=TOO_FAR), "end_too_far")
    assert env.calls == [] and env.cur.sql == [] and env.txns == 0


@pytest.mark.parametrize("make", [_grant, _team])
def test_a_grant_with_a_ddl_waiver_is_refused(env, make):
    d = refused(lambda: make(autoApprove=True, autoApproveTier="ddl", expiresAt=SOON),
                "tier_not_allowed")
    assert d["message"] == TIER_MESSAGE
    assert env.calls == [] and env.cur.sql == [] and env.txns == 0


def test_a_grant_with_auto_approve_and_an_end_goes_through(env):
    _grant(autoApprove=True, expiresAt=SOON)
    [call] = env.calls
    assert call["auto_approve_tier"] == "ro" and call["expires_at"] is not None


def test_a_team_grant_with_auto_approve_and_an_end_writes_the_waiver(env):
    _team(autoApprove=True, expiresAt=SOON, databases=["ledger"])
    waivers = [q for q, _ in env.cur.sql if q.startswith("INSERT INTO access_grant")
               and "TRUE, FALSE, NOW()" in q]
    assert len(waivers) == 1


def test_a_grant_with_no_waiver_may_still_have_no_end(env):
    """The limit is on the rule. A grant alone keeps its optional end."""
    _grant()
    [call] = env.calls
    assert call["auto_approve_tier"] is None and call["expires_at"] is None
    env.cur.sql.clear()
    _team()
    assert env.cur.sql                       # written, with no end and no waiver


# --- 4. POST /admin/grants/fleet ---------------------------------------------

def _fleet(**kw):
    body = {"subjectType": "user", "subject": "U0EXAMPLE002", "tier": "ro", "reason": "why"}
    body.update(kw)
    return ra.admin_add_fleet_grant(ra.FleetGrantIn(**body), claims=CLAIMS)


def test_the_fleet_route_refuses_auto_approve_with_no_end(env):
    refused(lambda: _fleet(autoApprove=True), "end_required")
    refused(lambda: _fleet(autoApprove=True, expiresAt=TOO_FAR), "end_too_far")
    assert env.cur.sql == [] and env.txns == 0


def test_the_fleet_route_refuses_a_ddl_waiver(env):
    d = refused(lambda: _fleet(autoApprove=True, tier="ddl", expiresAt=SOON), "tier_not_allowed")
    assert d["message"] == TIER_MESSAGE
    assert env.cur.sql == [] and env.txns == 0


def test_the_fleet_route_writes_the_waiver_with_an_end(env):
    env.cur.returning = [{"id": 5, "display_name": "Jordan Ray"}]
    out = _fleet(autoApprove=True, expiresAt=SOON)
    assert out["autoApprove"] is True
    inserts = [p for q, p in env.cur.sql if q.startswith("INSERT INTO access_grant")]
    assert len(inserts) == 2                              # the grant and its waiver
    assert all(p[3] is not None for p in inserts)         # valid_until: the end


def test_the_fleet_route_may_still_write_a_grant_with_no_waiver_and_no_end(env):
    env.cur.returning = [{"id": 5, "display_name": "Jordan Ray"}]
    out = _fleet()
    assert out["autoApprove"] is False
    [(q, p)] = [(q, p) for q, p in env.cur.sql if q.startswith("INSERT INTO access_grant")]
    assert p[3] is None                                  # valid_until


# --- 5. copy access ----------------------------------------------------------

class CopyCur:
    """Enough of a cursor for admin_copy_access, on the legacy model."""

    def __init__(self, rules):
        self.rules, self.sql, self._last = rules, [], ""

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = sql

    def fetchone(self):
        return {"n": 2, "skipped": 1}

    def fetchall(self):
        if "FROM auto_approve_grants" in self._last and "SELECT target_server_id" in self._last:
            return self.rules
        if "FROM target_servers" in self._last:
            return [{"id": 7, "alias": "prod-ledger"}, {"id": 8, "alias": "prod-orders"}]
        return []

    rowcount = 0


def _copy(env, rules, **kw):
    env.cur = CopyCur(rules)
    body = {"source": "U0EXAMPLE003", "includeTeams": False, "includeAutoApprove": True}
    body.update(kw)
    return ra.admin_copy_access("U0EXAMPLE002", ra.CopyAccessIn(**body), claims=CLAIMS)


def _copied(env):
    return [p for q, p in env.cur.sql if q.startswith("INSERT INTO auto_approve_grants")]


def rule(tid, tier="ro", end=None):
    return {"target_server_id": tid, "database_name": None, "max_tier": tier, "expires_at": end}


def test_a_copied_rule_with_no_end_gets_the_limit(env):
    env.v2 = False
    out = _copy(env, [rule(7)])
    [p] = _copied(env)
    end = p[5]
    assert end is not None
    assert abs((end - (datetime.now(timezone.utc) + timedelta(days=90))).total_seconds()) < 60
    assert out["autoApproveCopied"] == 1


def test_a_copied_rule_cannot_end_later_than_the_limit(env):
    env.v2 = False
    far = datetime.now(timezone.utc) + timedelta(days=400)
    _copy(env, [rule(7, end=far)])
    [p] = _copied(env)
    assert p[5] < far and p[5] <= datetime.now(timezone.utc) + timedelta(days=90, minutes=1)


def test_a_copied_rule_keeps_an_end_within_the_limit(env):
    env.v2 = False
    near = datetime.now(timezone.utc) + timedelta(days=10)
    _copy(env, [rule(7, end=near)])
    [p] = _copied(env)
    assert p[5] == near


def test_a_copied_ddl_rule_is_skipped_and_the_result_says_so(env):
    env.v2 = False
    out = _copy(env, [rule(7, "ddl"), rule(8, "rw", datetime.now(timezone.utc) + timedelta(days=5))])
    assert [p[3] for p in _copied(env)] == ["rw"]
    assert out["autoApproveCopied"] == 1 and out["autoApproveSkippedDdl"] == 1
    [(_, details)] = [a for a in env.audit if a[0] == "access_copied"]
    assert details["auto_approve_skipped_ddl"] == 1


def test_a_copy_dry_run_counts_the_rules_it_copies_and_the_ddl_ones_it_skips(env):
    env.v2 = False
    out = _copy(env, [], dryRun=True)
    assert out["wouldCopyAutoApprove"] == 2 and out["wouldSkipAutoApprove"] == 1
    assert _copied(env) == []


# --- 6. the approval of a window request -------------------------------------

def _window_env(monkeypatch, minutes, tier="ro"):
    import queryhub.admins as admins_mod
    import queryhub.targets as targets_mod
    req = {"id": 5, "status": "pending", "target_server_id": 7, "requester_slack_id": "U0EXAMPLE002",
           "max_tier": tier, "window_minutes": minutes, "database_name": None, "reason": "a release"}
    monkeypatch.setattr(admins_mod, "is_admin", lambda uid: True)
    monkeypatch.setattr(admins_mod, "can_approve", lambda uid, row: True)
    monkeypatch.setattr(targets_mod, "get", lambda tid: SimpleNamespace(alias="prod-ledger"))
    monkeypatch.setattr(aar, "get", lambda rid: req)
    monkeypatch.setattr(aar, "needs_super_admin", lambda r, alias: False)
    monkeypatch.setattr(aar.idp_outbox, "enabled", lambda: False)
    ran = []

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def cursor(self): return self
        def execute(self, sql, params=None): ran.append((sql, params))
        def fetchone(self): return {"id": 9, "expires_at": None}
        def commit(self): ran.append(("COMMIT", None))
        def rollback(self): pass
        rowcount = 1

    monkeypatch.setattr(aar.db, "connection", lambda: Conn())
    return ran


def _decide():
    return aar.decide_window(5, approve=True, actor_id="U0EXAMPLE001", actor_name="Example Admin")


def test_a_window_longer_than_the_limit_is_refused_and_nothing_is_written(monkeypatch):
    ran = _window_env(monkeypatch, 30 * 1440)
    _setting(monkeypatch, 7)
    with pytest.raises(aar.WindowDecisionRefused) as e:
        _decide()
    assert e.value.code == "window_too_long" and e.value.status == 409
    assert "at most 7 days" in e.value.message and "reject" in e.value.message
    assert ran == []


def test_a_window_within_the_limit_is_written_and_always_has_an_end(monkeypatch):
    ran = _window_env(monkeypatch, 60)
    out = _decide()
    assert out["status"] == "approved"
    [(sql, params)] = [r for r in ran if r[0].startswith("INSERT INTO auto_approve_grants")]
    assert "NOW() + make_interval(mins => %s)" in sql and 60 in params


def test_a_window_request_row_with_no_length_is_refused(monkeypatch):
    ran = _window_env(monkeypatch, 0)
    with pytest.raises(aar.WindowDecisionRefused) as e:
        _decide()
    assert e.value.code == "no_window"
    assert ran == []


def test_a_ddl_window_request_is_refused_by_its_own_rule(monkeypatch):
    ran = _window_env(monkeypatch, 60, tier="ddl")
    with pytest.raises(aar.WindowDecisionRefused) as e:
        _decide()
    assert e.value.code == "ddl"
    assert ran == []


# --- 7. the insert helpers, called directly ----------------------------------

@pytest.mark.parametrize("insert", [
    lambda cur, **kw: ra._insert_user_waiver(cur, "U0EXAMPLE002", kw.get("tier", "ro"), 53, None,
                                             kw.get("end"), kw.get("minutes"), None, "U0EXAMPLE001"),
    lambda cur, **kw: ra._insert_team_waiver(cur, 7, kw.get("tier", "ro"), 53, None,
                                             kw.get("end"), kw.get("minutes"), None, "U0EXAMPLE001"),
])
def test_the_insert_helpers_refuse_a_rule_with_no_end_or_ddl_and_write_nothing_then(insert):
    cur = Cur()
    refused(lambda: insert(cur), "end_required")
    refused(lambda: insert(cur, end=TOO_FAR), "end_too_far")
    refused(lambda: insert(cur, end=SOON, tier="ddl"), "tier_not_allowed")
    assert cur.sql == []
    insert(cur, end=SOON)
    insert(cur, minutes=30, tier="rw")
    assert len(cur.sql) == 2


# --- the list says the limit; the screen refuses a limit of zero --------------

def test_the_list_says_the_limit(env, monkeypatch):
    assert ra.admin_auto_grants(claims=CLAIMS)["maxDays"] == 90
    _setting(monkeypatch, 30)
    out = ra.admin_auto_grants(claims=CLAIMS)
    assert out["maxDays"] == 30 and out["autoGrants"] == []


class ConfigCur:
    def __init__(self):
        self.updates = []

    def execute(self, sql, params=None):
        if sql.startswith("UPDATE"):
            self.updates.append(params)

    def fetchall(self):
        return [{"key": "auto_approve_max_days", "value": "90"}]


@pytest.mark.parametrize("raw", [0, "", -1, 366])
def test_a_config_save_that_would_switch_the_limit_off_is_a_400(env, raw):
    env.cur = ConfigCur()
    d = refused(lambda: ra.admin_save_config(ra.ConfigIn(changes={"auto_approve_max_days": raw}),
                                             claims=CLAIMS), "bad_request")
    assert "from 1 to 365" in d["message"]
    assert env.cur.updates == []
