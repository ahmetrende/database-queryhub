"""A grant can carry auto-approve, written in the SAME transaction.

Granting access and then adding auto-approve for the same person was two
screens and two transactions, so the pair could be half-applied: the grant
written and the waiver not, or the reverse. `grants.grant(...,
auto_approve_tier=...)` writes both on one cursor.

Two rules ride with it, both pinned here:

* the waiver may not claim more than the grant gives (and never DDL);
* a waiver the person already holds, equal or broader, is not written again.
  28 per-server waivers once sat under a fleet-wide one that made every one of
  them redundant.

No database: the cursor below records what would have been written.
"""
import contextlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from queryhub import grants

ADMIN, DEV = "U0EXAMPLE001", "U0EXAMPLE002"
NOW = datetime.now(timezone.utc)
PAST, SOON, LATER = NOW - timedelta(days=1), NOW + timedelta(days=3), NOW + timedelta(days=30)


class Cur:
    """Records every statement; can be told to fail on the waiver insert."""

    def __init__(self):
        self.fail_waiver = False
        self.statements, self.grants_written, self.waivers, self.audits = [], [], [], []
        self._row = None
        self._next = 500

    def execute(self, sql, params=()):
        self._row = None
        if "INSERT INTO requesters" in sql:
            self.statements.append("whitelist")
            self._row = {"inserted": False}
        elif "INSERT INTO user_target_grants" in sql:
            self.statements.append("grant")
            self.grants_written.append(params)
            self._row = {"mode": params[3], "allowed_databases": params[2],
                         "expires_at": params[5]}
        elif "INSERT INTO auto_approve_grants" in sql:
            self.statements.append("waiver")
            if self.fail_waiver:
                raise RuntimeError("check constraint violated")
            self._next += 1
            self.waivers.append(params)
            self._row = {"id": self._next}
        elif "INSERT INTO audit_log" in sql:
            action = sql.split("'")[1]
            self.statements.append("audit:" + action)
            self.audits.append((action, json.loads(params[2])))

    def fetchone(self):
        return self._row


@pytest.fixture
def env(monkeypatch):
    st = SimpleNamespace(cur=Cur(), txns=0, committed=False, held=[], notified=[])

    @contextlib.contextmanager
    def txn():
        st.txns += 1
        yield st.cur
        st.committed = True                 # reached only without an exception

    monkeypatch.setattr(grants.db, "transaction", txn)
    monkeypatch.setattr(grants, "control_plane_target_ids", lambda: {99})
    monkeypatch.setattr(grants.auto_approve, "active_grants", lambda pid, at=None: st.held)
    monkeypatch.setattr(grants, "notify_grantee",
                        lambda gid, *a, **k: st.notified.append((gid, k.get("auto_approve_tier"))))
    monkeypatch.setattr("queryhub.targets.get",
                        lambda tid: SimpleNamespace(alias={7: "prod-orders", 8: "prod-ledger"}.get(tid, f"t{tid}")))
    return st


def _grant(**kw):
    args = dict(granter_id=ADMIN, granter_name="Example Admin", grantee_id=DEV,
                grantee_profile={}, target_id=7, mode="ro", databases=None,
                reason=None, notify=True, auto_approve_tier="ro")
    args.update(kw)
    return grants.grant(**args)


def _waiver(**kw):
    row = {"id": 12, "slack_user_id": DEV, "max_tier": "ro", "target_server_id": None,
           "database_name": None, "starts_at": PAST, "expires_at": None}
    row.update(kw)
    return row


# --- one transaction ---------------------------------------------------------

def test_the_grant_and_its_waiver_are_written_in_one_transaction(env):
    out = _grant(reason="onboarding", expires_at=LATER)
    assert env.txns == 1 and env.committed
    assert env.cur.statements == ["whitelist", "grant", "audit:access_granted",
                                  "waiver", "audit:auto_approve_granted"]
    [w] = env.cur.waivers
    assert w == (DEV, "ro", 7, None, LATER, "auto-approve with the grant: onboarding", ADMIN)
    assert out["auto_approve"] == {"tier": "ro", "skipped": [],
                                   "written": [{"id": "501", "database": None}]}
    audit = dict(env.cur.audits)["auto_approve_granted"]
    assert audit["with_grant"] is True and audit["grant_id"] == 501


def test_a_waiver_that_fails_takes_the_grant_with_it(env):
    env.cur.fail_waiver = True
    with pytest.raises(RuntimeError):
        _grant()
    # Same cursor, same transaction: the grant row was executed but never
    # committed, and nobody is told they were granted anything.
    assert "grant" in env.cur.statements
    assert env.committed is False
    assert env.notified == []


def test_without_a_tier_nothing_about_auto_approve_happens(env):
    out = _grant(auto_approve_tier=None)
    assert "waiver" not in env.cur.statements
    assert out["auto_approve"] is None
    assert "auto_approve" not in dict(env.cur.audits)["access_granted"]
    assert env.notified == [(DEV, None)]


def test_the_reason_without_one_is_still_said(env):
    _grant(reason="  ")
    assert env.cur.waivers[0][5] == "auto-approve with the grant"


# --- the tier cap ------------------------------------------------------------

@pytest.mark.parametrize("mode,tier", [
    ("ro", "rw"),        # above the grant it comes with
    ("ro", "ddl"),
    ("rw", "ddl"),
    ("ddl", "ddl"),      # DDL is never waived, whatever the grant
    ("rw", "admin"),     # not a tier at all
])
def test_a_waiver_tier_above_the_grant_or_ddl_is_refused_before_anything_runs(env, mode, tier):
    with pytest.raises(ValueError):
        _grant(mode=mode, auto_approve_tier=tier)
    assert env.txns == 0 and env.cur.statements == []
    with pytest.raises(ValueError):
        grants.grant_many(granter_id=ADMIN, granter_name=None, grantees=[(DEV, {})],
                          target_id=7, mode=mode, databases=None, reason=None,
                          auto_approve_tier=tier)
    assert env.txns == 0


@pytest.mark.parametrize("mode,tier", [("ro", "ro"), ("rw", "ro"), ("rw", "rw"),
                                       ("ddl", "rw"), ("DDL", "RO")])
def test_a_waiver_at_or_below_the_grant_is_accepted(mode, tier):
    assert grants.check_waiver_tier(mode, tier) == tier.lower()


# --- skip when covered -------------------------------------------------------

def test_a_fleet_wide_waiver_covers_it_so_none_is_written(env):
    env.held = [_waiver(id=12)]
    out = _grant(databases=["app"], expires_at=LATER)
    assert "waiver" not in env.cur.statements
    [s] = out["auto_approve"]["skipped"]
    assert s["database"] == "app" and s["covered_by"] == "12"
    assert "every server they can reach" in s["reason"] and "#12" in s["reason"]
    # The grant's own audit row is where the trail records it.
    access = dict(env.cur.audits)["access_granted"]
    assert access["auto_approve"] == "ro" and access["auto_approve_covered_by"] == ["12"]
    # Nothing new, so the DM does not announce auto-approve as news.
    assert env.notified == [(DEV, None)]


def test_a_same_scope_waiver_covers_only_its_own_database(env):
    env.held = [_waiver(id=13, max_tier="rw", target_server_id=7, database_name="app",
                        expires_at=LATER)]
    out = _grant(databases=["app", "billing"], expires_at=SOON)
    assert [w[3] for w in env.cur.waivers] == ["billing"]
    assert [s["database"] for s in out["auto_approve"]["skipped"]] == ["app"]
    assert "prod-orders/app" in out["auto_approve"]["skipped"][0]["reason"]
    assert env.notified == [(DEV, "ro")]


def test_an_all_databases_waiver_on_the_server_covers_every_database(env):
    env.held = [_waiver(id=14, target_server_id=7, database_name=None)]
    out = _grant(databases=["app", "billing"])
    assert env.cur.waivers == []
    assert len(out["auto_approve"]["skipped"]) == 2


@pytest.mark.parametrize("held,kw", [
    (dict(expires_at=SOON), dict(expires_at=LATER)),               # ends first
    (dict(expires_at=SOON), dict(expires_at=None)),                # ends at all
    (dict(max_tier="ro"), dict(mode="rw", auto_approve_tier="rw")),  # narrower tier
    (dict(target_server_id=8), {}),                                # another server
    (dict(target_server_id=7, database_name="other"), dict(databases=["app"])),
    (dict(target_server_id=7, database_name="app"), dict(databases=None)),  # one db only
    (dict(starts_at=SOON), {}),                                    # not started
    (dict(expires_at=PAST), {}),                                   # already over
    # A TEAM's waiver stops reaching them once their own grant is on the
    # server (access.py rule 4), so it cannot stand in for theirs.
    (dict(team_id=5, id="ag:9"), {}),
])
def test_what_does_not_cover_it_is_written(env, held, kw):
    env.held = [_waiver(**held)]
    out = _grant(**kw)
    assert len(env.cur.waivers) == 1, held
    assert out["auto_approve"]["skipped"] == []


def test_the_broadest_covering_row_is_the_one_named(env):
    env.held = [_waiver(id=20, target_server_id=7, database_name="app"),
                _waiver(id=21, target_server_id=7),
                _waiver(id=22)]
    out = _grant(databases=["app"])
    assert out["auto_approve"]["skipped"][0]["covered_by"] == "22"


# --- one waiver per database -------------------------------------------------

def test_one_waiver_per_granted_database(env):
    _grant(databases=["app", "billing", "app"])
    assert [w[3] for w in env.cur.waivers] == ["app", "billing"]


@pytest.mark.parametrize("dbs", [None, [], ["*"], ["app", "*"], [" ALL "]])
def test_every_database_is_one_waiver_with_no_name(env, dbs):
    _grant(databases=dbs)
    assert [w[3] for w in env.cur.waivers] == [None]
    # ...and the grant row agrees: `*` is every database, not a database named `*`.
    assert env.cur.grants_written[0][2] is None


# --- several people at once --------------------------------------------------

def test_grant_many_decides_coverage_per_person(env, monkeypatch):
    held = {DEV: [_waiver(id=30)], "U0EXAMPLE003": []}
    monkeypatch.setattr(grants.auto_approve, "active_grants", lambda pid, at=None: held[pid])
    out = grants.grant_many(granter_id=ADMIN, granter_name=None,
                            grantees=[(DEV, {}), ("U0EXAMPLE003", {})], target_id=7,
                            mode="rw", databases=None, reason=None,
                            auto_approve_tier="ro")
    assert env.txns == 1 and env.committed
    assert [w[0] for w in env.cur.waivers] == ["U0EXAMPLE003"]
    assert out[0]["auto_approve"]["skipped"][0]["covered_by"] == "30"
    assert out[1]["auto_approve"]["written"] == [{"id": "501", "database": None}]
    # Only the person who got a new waiver is told about it.
    assert env.notified == [(DEV, None), ("U0EXAMPLE003", "ro")]


# --- the message -------------------------------------------------------------

def test_the_grantee_message_says_auto_approve_only_when_asked():
    plain = grants.grant_message(["prod-orders"], "rw", None)
    assert "without waiting for approval" not in plain
    one = grants.grant_message(["prod-orders"], "rw", None, auto_approve_tier="ro")
    assert "Queries up to *RO* there run without waiting for approval." in one
    many = grants.grant_message(["prod-orders", "prod-ledger"], "rw", None,
                                auto_approve_tier="ro")
    assert "on these run without waiting" in many


def test_covering_needs_no_database():
    """The pure predicate, on the shapes the two callers hand it."""
    fleet = _waiver()
    assert grants.covering_waiver([fleet], "ro", 7, None) is fleet
    assert grants.covering_waiver([fleet], "rw", 7, None) is None
    team_shaped = {"id": "ag:3", "max_tier": "rw", "target_server_id": 7,
                   "database_name": "app", "starts_at": PAST, "expires_at": None}
    assert grants.covering_waiver([team_shaped], "ro", 7, "app", LATER) is team_shaped
    assert grants.covering_waiver([team_shaped], "ro", 7, None) is None
