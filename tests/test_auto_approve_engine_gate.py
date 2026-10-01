"""Who skips review on an Athena archive, and how every surface agrees on it.

Operator decision C3 (2026-09-20): a query on an Athena archive is reviewed by a
person, because what it costs depends on the partitions it reads. The rule for
an archive (operator, 2026-10-01) says who is trusted to skip that review:

1. except super-admins, nobody holds more than RO there (the engine refuses a
   write for everyone; test_archive_tier_ceiling.py has the screens);
2. the lead of the team that owns the archive auto-approves RO, waiver or not
   (an approver role scoped to it, `access.approves_target`);
3. so does anyone holding an admin role -- any admin, not only a super-admin --
   and anyone whose fleet-wide RO waiver covers the request; a waiver naming
   the archive still applies;
4. everyone else goes to normal approval, and the fingerprint cache stays off,
   so a member's repeat query is still reviewed;
5. `engine_config.auto_approve: true` behaves like any RDS target (waivers and
   the cache, no role rule), `false` lets nothing through but a super-admin's
   own submission; window requests stay refused on an archive; the
   super-admin path is untouched.

`auto_approve.decision` is the one answer. `create_request` decides with it and
everything that announces it asks it too; the last tests read the package and
fail a caller of `effective_grant` that goes around it.
"""
from __future__ import annotations

import ast
import contextlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from queryhub import (access, athena_exec, auto_approve, bundles, core_submit as cs, db,
                           engines, executor, grants, lifecycle, targets, teams)

LEAD = "U0EXAMPLE001"       # leads the team that owns the archive; fleet-wide waiver
CAPTAIN = "U0EXAMPLE002"    # reaches every target, fleet-wide waiver, no role
MEMBER = "U0EXAMPLE003"     # on the owning team; no waiver, no role
NAMED = "U0EXAMPLE004"      # holds a waiver that names the archive
BOTH = "U0EXAMPLE005"       # a fleet-wide waiver AND one that names the archive
ADMIN = "U0EXAMPLE006"      # an RO-capped admin role, no waiver
LEAD_BARE = "U0EXAMPLE007"  # leads the owning team, holds no waiver
SUPER = "U0EXAMPLE008"      # a super-admin, no waiver
EXPIRING = "U0EXAMPLE010"   # a waiver naming the archive that ends tomorrow
READ = "SELECT count(*) FROM ledgers WHERE day = DATE '2026-09-01'"
NOW = datetime.now(timezone.utc)
_IDS = {"postgres": 7, "mssql": 8, "clickhouse": 9, "athena": 40}


def _target(engine="postgres", engine_config=None):
    return targets.TargetServer(
        id=_IDS[engine], alias=f"example-{engine}", host=f"{engine}.example.test",
        port=443, default_database="ledger", username="reader", enabled=True,
        notes=None, engine=engine, engine_config=dict(engine_config or {}))


ARCHIVE = _target("athena")
ARCHIVE_ON = _target("athena", {"auto_approve": True})
ARCHIVE_OFF = _target("athena", {"auto_approve": False})
LEDGER = _target("postgres")
OTHER_ENGINES = ["postgres", "mssql", "clickhouse"]


def _waiver(wid, holder, target_id=None, expires_at=None, team_id=None, tier="ro"):
    row = {"id": wid, "slack_user_id": holder, "max_tier": tier,
           "target_server_id": target_id, "database_name": None,
           "starts_at": NOW - timedelta(days=1), "expires_at": expires_at,
           "reason": "example", "granted_by": "U0EXAMPLE009"}
    if team_id is not None:
        row.update(team_id=team_id, team_name="example team")
    return row


def _role(role, target_id=None, *, tier="ro", any_tier=False):
    """A `role_assignment` row as `access.roles` returns it."""
    return {"role": role, "scope_team_id": None, "all_teams": True,
            "scope_target_id": target_id, "all_targets": target_id is None,
            "max_tier": None if any_tier else tier, "any_tier": any_tier,
            "valid_until": None}


# A fleet-wide read waiver with no expiry (target and database NULL), the shape
# a pod captain and a fleet-wide reader both hold.
WIDE = {who: _waiver(wid, who) for wid, who in ((501, LEAD), (502, CAPTAIN), (504, BOTH))}
ON_ARCHIVE = {NAMED: _waiver(503, NAMED, ARCHIVE.id),
              BOTH: _waiver(505, BOTH, ARCHIVE.id, expires_at=NOW + timedelta(days=1)),
              EXPIRING: _waiver(508, EXPIRING, ARCHIVE.id, expires_at=NOW + timedelta(days=1))}
HELD = {LEAD: [WIDE[LEAD]], CAPTAIN: [WIDE[CAPTAIN]], MEMBER: [],
        NAMED: [ON_ARCHIVE[NAMED]], BOTH: [ON_ARCHIVE[BOTH], WIDE[BOTH]],
        ADMIN: [], LEAD_BARE: [], SUPER: [], EXPIRING: [ON_ARCHIVE[EXPIRING]]}
ROLES = {LEAD: [_role("approver", ARCHIVE.id)], LEAD_BARE: [_role("approver", ARCHIVE.id)],
         ADMIN: [_role("admin")], SUPER: [_role("admin", any_tier=True)]}
PRIOR = {"id": 77, "completed_at": NOW}


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """Nothing here may reach a database: a read a fixture forgot to fake
    fails the test instead of asking whatever the environment points at."""
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)


@pytest.fixture
def world(monkeypatch):
    """Who holds which waiver, and which roles.

    Faked at the bottom -- the live waiver rows and `access.roles` -- so the
    real `decision`, `effective_grant`, `waiver_applies`, `archive_role`,
    `approves_target` and `is_super_admin` decide. `asked` records every time
    the role half of the archive rule is consulted. The spy is set with
    raising=False so this fixture also runs against a tree that lacks the
    helper, and the tests below fail on their assertions there.
    """
    st = {"held": {k: list(v) for k, v in HELD.items()},
          "roles": {k: list(v) for k, v in ROLES.items()}, "asked": []}

    def active(pid, at=None):
        at = at or datetime.now(timezone.utc)
        return [w for w in st["held"].get(pid, [])
                if w["starts_at"] <= at and (w["expires_at"] is None or w["expires_at"] > at)]

    real_role = getattr(access, "archive_role", None)

    def role(pid, tid):
        st["asked"].append((pid, tid))
        return real_role(pid, tid) if real_role else None
    monkeypatch.setattr(auto_approve, "active_grants", active)
    monkeypatch.setattr(access, "roles", lambda pid: st["roles"].get(pid, []))
    monkeypatch.setattr(access, "archive_role", role, raising=False)
    return st


def _super(uid):
    return access.is_super_admin(uid)


# ---------------------------------------------------------------------------
# the target's own answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("engine,expected", [
    ("postgres", True), ("mssql", True), ("clickhouse", True), ("athena", False),
])
def test_each_engine_has_a_default_and_only_athena_is_off(engine, expected):
    assert engines.spec(engine).auto_approve_default is expected
    assert engines.auto_approve_allowed(_target(engine)) is expected
    assert engines.archive_rule_applies(_target(engine)) is (not expected)


def test_a_target_overrides_its_engine_in_both_directions():
    assert engines.auto_approve_allowed(ARCHIVE_ON) is True
    assert engines.auto_approve_allowed(_target("postgres", {"auto_approve": False})) is False
    assert engines.auto_approve_override(ARCHIVE_ON) is True
    assert engines.auto_approve_override(ARCHIVE_OFF) is False
    assert engines.auto_approve_override(ARCHIVE) is None


def test_only_an_archive_that_says_nothing_is_under_the_archive_rule():
    assert engines.archive_rule_applies(ARCHIVE) is True
    assert engines.archive_rule_applies(ARCHIVE_ON) is False
    assert engines.archive_rule_applies(ARCHIVE_OFF) is False
    assert engines.archive_rule_applies(_target("postgres", {"auto_approve": False})) is False
    assert engines.archive_rule_applies(None) is False


@pytest.mark.parametrize("value", ["true", "false", "yes", 1, 0, None, [], {}])
def test_only_a_json_boolean_is_an_answer(value):
    """A string that reads as true, or a 1, is not a decision anybody made: it
    falls back to the engine's default instead of opening the door."""
    t = _target("athena", {"auto_approve": value})
    assert engines.auto_approve_override(t) is None
    assert engines.auto_approve_allowed(t) is False
    assert engines.archive_rule_applies(t) is True
    assert engines.auto_approve_allowed(_target("postgres", {"auto_approve": value})) is True


def test_no_target_means_no_exemption():
    assert engines.auto_approve_allowed(None) is False
    assert auto_approve.decision(LEAD, "ro", None, "ledger", rows=[WIDE[LEAD]]) is None


def test_an_engine_config_that_is_not_an_object_reads_as_the_default():
    class Odd:
        engine, engine_config = "athena", ["auto_approve", True]
    assert engines.auto_approve_allowed(Odd()) is False


# ---------------------------------------------------------------------------
# which waiver applies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("waiver,expected", [
    (WIDE[CAPTAIN], True),                             # anyone's fleet-wide waiver
    (WIDE[LEAD], True),
    (ON_ARCHIVE[NAMED], True),                         # one that names the archive
    (_waiver(506, NAMED, LEDGER.id), False),           # one that names another target
    (_waiver("ag:9", MEMBER, team_id=17), True),       # a team's (reach: effective_grant's)
    (None, False),
], ids=["captain", "lead", "named", "named-elsewhere", "team", "none"])
def test_on_an_archive_every_waiver_that_covers_it_applies(world, waiver, expected):
    assert auto_approve.waiver_applies(ARCHIVE, waiver) is expected
    assert world["asked"] == [], "a waiver is not a role question"


def test_a_target_that_turns_it_on_lets_every_waiver_apply():
    for w in (WIDE[CAPTAIN], ON_ARCHIVE[NAMED], _waiver(506, NAMED, LEDGER.id)):
        assert auto_approve.waiver_applies(ARCHIVE_ON, w) is True


def test_a_target_that_turns_it_off_lets_none_apply():
    assert auto_approve.waiver_applies(ARCHIVE_OFF, WIDE[LEAD]) is False
    assert auto_approve.waiver_applies(ARCHIVE_OFF, ON_ARCHIVE[NAMED]) is False


@pytest.mark.parametrize("engine", OTHER_ENGINES)
def test_other_engines_let_every_waiver_apply(engine):
    t = _target(engine)
    assert auto_approve.waiver_applies(t, WIDE[CAPTAIN]) is True
    assert auto_approve.waiver_applies(t, _waiver(507, NAMED, t.id)) is True


def test_no_target_or_no_waiver_means_no():
    assert auto_approve.waiver_applies(None, WIDE[LEAD]) is False
    assert auto_approve.waiver_applies(ARCHIVE, None) is False
    assert auto_approve.waiver_applies(LEDGER, None) is False


# ---------------------------------------------------------------------------
# the role half: who it is, and only for a read on an archive
# ---------------------------------------------------------------------------

def test_the_lead_is_an_approver_role_that_names_the_target(monkeypatch):
    """`access.approves_target` reads the rows `roles` returns, which are
    already live, unrevoked and dropped for a disabled principal."""
    rows = {
        LEAD: [_role("approver", ARCHIVE.id)],
        CAPTAIN: [_role("approver"), _role("granter", ARCHIVE.id)],
    }
    monkeypatch.setattr(access, "roles", lambda pid: rows.get(pid, []))
    assert access.approves_target(LEAD, ARCHIVE.id) is True
    assert access.approves_target(LEAD, LEDGER.id) is False
    assert access.approves_target(CAPTAIN, ARCHIVE.id) is False
    assert access.approves_target(MEMBER, ARCHIVE.id) is False


@pytest.mark.parametrize("rows,expected", [
    ([_role("approver", ARCHIVE.id)], "owner lead"),
    ([_role("admin")], "admin"),                                   # an RO-capped admin
    ([_role("admin", LEDGER.id)], "admin"),                        # any admin role
    ([_role("admin"), _role("approver", ARCHIVE.id)], "owner lead"),
    ([_role("approver", LEDGER.id)], None),                        # leads another target
    ([_role("approver")], None),                                   # approves every target
    ([_role("granter", ARCHIVE.id)], None),
    ([_role("admin", any_tier=True)], None),                       # the super-admin rule's
    ([], None),
], ids=["lead", "admin", "scoped-admin", "both", "lead-elsewhere", "all-approver",
        "granter", "super-admin", "nothing"])
def test_which_role_lets_a_read_through(monkeypatch, rows, expected):
    calls = []
    monkeypatch.setattr(access, "roles", lambda pid: calls.append(pid) or rows)
    assert access.archive_role(LEAD, ARCHIVE.id) == expected
    assert calls == [LEAD], "one read of the roles"


@pytest.mark.parametrize("user,basis", [(ADMIN, "archive: admin"),
                                        (LEAD_BARE, "archive: owner lead")])
def test_a_role_lets_a_read_through_with_no_waiver(world, user, basis):
    got = auto_approve.decision(user, "ro", ARCHIVE, "ledger")
    assert got["basis"] == basis and got["id"] is None and got["max_tier"] == "ro"


def test_a_waiver_decides_before_the_role_is_asked(world):
    got = auto_approve.decision(LEAD, "ro", ARCHIVE, "ledger")
    assert got is WIDE[LEAD]
    assert world["asked"] == []


@pytest.mark.parametrize("target", [ARCHIVE_ON, ARCHIVE_OFF, LEDGER, _target("mssql"),
                                    _target("clickhouse")],
                         ids=["athena-on", "athena-off", *OTHER_ENGINES])
def test_the_role_half_is_the_archive_rules_only(world, target):
    assert auto_approve.decision(ADMIN, "ro", target, "ledger") is None
    assert auto_approve.decision(LEAD_BARE, "ro", target, "ledger") is None
    assert world["asked"] == []


def test_on_an_archive_only_a_read_is_let_through(world):
    """Except super-admins, nobody holds more than RO there."""
    world["held"][CAPTAIN] = [_waiver(510, CAPTAIN, tier="ddl")]
    for mode in ("rw", "ddl"):
        assert auto_approve.decision(CAPTAIN, mode, ARCHIVE, "ledger") is None
        assert auto_approve.decision(ADMIN, mode, ARCHIVE, "ledger") is None
    assert auto_approve.decision(CAPTAIN, "ro", ARCHIVE, "ledger")["id"] == 510
    assert auto_approve.decision(CAPTAIN, "rw", LEDGER, "ledger")["id"] == 510


def test_nobody_else_is_let_through(world):
    assert auto_approve.decision(MEMBER, "ro", ARCHIVE, "ledger") is None
    assert world["asked"] == [(MEMBER, ARCHIVE.id)]


# ---------------------------------------------------------------------------
# effective_grant: the filter picks, it does not merely veto
# ---------------------------------------------------------------------------

def test_a_refused_row_is_passed_over_so_a_narrower_one_can_decide():
    """The fleet-wide row has no expiry, so it sorts first; checking only the
    answer would refuse it and stop there."""
    wide, named = WIDE[BOTH], ON_ARCHIVE[BOTH]
    rows = [named, wide]
    ask = dict(target_server_id=ARCHIVE.id, database_name="ledger", rows=rows)
    assert auto_approve.effective_grant(BOTH, "ro", **ask) is wide
    assert auto_approve.effective_grant(
        BOTH, "ro", **ask, applies=lambda g: g is not wide) is named
    assert auto_approve.effective_grant(BOTH, "ro", **ask, applies=lambda g: False) is None


def test_the_filter_never_sees_a_team_row_that_does_not_reach(monkeypatch):
    """So a caller that keeps the refused rows, to tell their holder why, only
    keeps waivers the holder really has here."""
    monkeypatch.setattr(auto_approve, "_team_waiver_applies", lambda *a: False)
    seen = []
    got = auto_approve.effective_grant(
        MEMBER, "ro", ARCHIVE.id, "ledger", rows=[_waiver("ag:9", MEMBER, team_id=17)],
        applies=lambda g: seen.append(g) or True)
    assert got is None and seen == []


# ---------------------------------------------------------------------------
# create_request: the decision, and how it is recorded
# ---------------------------------------------------------------------------

class _Cur:
    """Answers the create transaction's statements by what they are: no open
    requests, no duplicate, and id 42 for the INSERT."""

    def __init__(self, box):
        self.box, self.rowcount, self.last = box, 1, ""

    def execute(self, sql, params=None):
        self.last = " ".join(sql.split())
        self.box["sql"].append((self.last, params))

    def fetchone(self):
        if self.last.startswith("SELECT count(*)"):
            return {"n": 0}
        if self.last.startswith("INSERT INTO requests"):
            return {"id": 42}
        return None


def _patch_create(monkeypatch, st):
    @contextlib.contextmanager
    def txn():
        yield _Cur(st)
    monkeypatch.setattr(cs.db, "transaction", txn)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.admins, "is_super_admin", _super)

    def cache_hit(*a, **k):
        st["cache_asked"] += 1
        return st["prior"]
    monkeypatch.setattr(cs.auto_approve, "fingerprint_cache_hit", cache_hit)
    monkeypatch.setattr(cs.ast_safety, "fingerprint", lambda *a, **k: "fp-1")
    monkeypatch.setattr(cs.audit, "log_in",
                        lambda cur, rid, actor, name, action, details=None:
                        st["audit"].append((action, details)))


def _prepared(target, user, sched_for=None):
    return cs.Prepared(
        user_id=user, user_name="Ex", target=target, database="ledger",
        query=READ, required_mode="ro", justification=None, wants_result=True,
        result_format="csv", sched_for=sched_for, explain_plan=None,
        risk_summary=None, origin="web")


@pytest.fixture
def create(monkeypatch, world):
    st = world
    st.update({"prior": None, "cache_asked": 0, "sql": [], "audit": []})
    _patch_create(monkeypatch, st)

    def go(target, user, sched_for=None):
        st["sql"].clear()
        st["audit"].clear()
        out = cs.create_request(_prepared(target, user, sched_for))
        assert isinstance(out, cs.Outcome), out
        insert = next((s, p) for s, p in st["sql"] if s.startswith("INSERT INTO requests"))
        return out, insert
    st["go"] = go
    return st


def _actions(st):
    return [action for action, _details in st["audit"]]


def _columns(sql):
    """The INSERT's column list. Not the whole statement: its RETURNING names
    the decision columns on both paths."""
    return sql[sql.index("(") + 1:sql.index(")")].replace(" ", "").split(",")


def _granted(out):
    return out.aa_grant["id"] if out.aa_grant else None


@pytest.mark.parametrize("user,grant,basis", [
    (LEAD, 501, None),                        # the lead's own waiver decides, as before
    (LEAD_BARE, None, "archive: owner lead"),  # rule 2: no waiver needed
    (ADMIN, None, "archive: admin"),           # rule 3: any admin
    (CAPTAIN, 502, None),                     # rule 3: anyone's fleet-wide waiver
    (NAMED, 503, None),                       # a waiver naming the archive
    (BOTH, 504, None),
], ids=["lead", "lead-no-waiver", "admin", "captain", "named", "both"])
def test_these_reads_on_the_archive_skip_review(create, user, grant, basis):
    out, (sql, _params) = create["go"](ARCHIVE, user)
    assert out.auto_approved is True and out.fp_hit is None and out.aa_warn is None
    assert (_granted(out), out.aa_grant.get("basis")) == (grant, basis)
    assert "decided_by_slack_id" in _columns(sql)
    assert _actions(create) == ["submitted", "auto_approved"]


@pytest.mark.parametrize("user,basis", [(ADMIN, "archive: admin"),
                                        (LEAD_BARE, "archive: owner lead")])
def test_a_role_decision_names_the_role_and_no_grant(create, user, basis):
    _out, (_sql, params) = create["go"](ARCHIVE, user)
    details = dict(create["audit"])
    assert details["submitted"]["basis"] == basis
    assert "grant_id" not in details["submitted"]
    assert details["auto_approved"] == {"basis": basis, "max_tier": "ro",
                                        "scheduled_for": None}
    reason = f"auto-approved ({basis}, max_tier=ro)"
    assert params.count(reason) == 2, "decided_by_name and decision_reason"
    assert auto_approve.AUTO_DECIDED_BY in params


def test_a_waiver_decision_still_names_the_waiver(create):
    create["go"](ARCHIVE, CAPTAIN)
    details = dict(create["audit"])
    assert details["auto_approved"]["grant_id"] == 502
    assert "basis" not in details["auto_approved"]


def test_a_member_with_no_waiver_and_no_role_waits_and_is_told_nothing(create):
    out, (sql, _params) = create["go"](ARCHIVE, MEMBER)
    assert out.auto_approved is False and out.aa_grant is None and out.aa_warn is None
    assert "decided_by_slack_id" not in _columns(sql), "the pending INSERT"
    assert _actions(create) == ["submitted"]
    assert "auto_approved" not in create["audit"][0][1]


@pytest.mark.parametrize("user", [MEMBER, CAPTAIN, ADMIN])
def test_a_fingerprint_match_never_applies_on_the_archive(create, user):
    create["prior"] = PRIOR
    out, (_sql, params) = create["go"](ARCHIVE, user)
    assert out.fp_hit is None and create["cache_asked"] == 0
    assert out.auto_approved is (user != MEMBER), "only by waiver or role"
    # Still computed and stored: the row reads like any other request's.
    assert "fp-1" in params


def test_a_scheduled_run_needs_its_waiver_then_too(create):
    """The waiver ends before the run, so the run waits; an admin's role does
    not end with a waiver, so theirs still runs."""
    later = NOW + timedelta(days=2)
    out, _insert = create["go"](ARCHIVE, EXPIRING, sched_for=later)
    assert out.auto_approved is False
    assert out.aa_warn and "expires before the scheduled run time" in out.aa_warn
    create["held"][ADMIN] = [_waiver(509, ADMIN, ARCHIVE.id, expires_at=NOW + timedelta(days=1))]
    out, _insert = create["go"](ARCHIVE, ADMIN, sched_for=later)
    assert out.auto_approved is True and _granted(out) == 509
    out, _insert = create["go"](ARCHIVE, LEAD_BARE, sched_for=later)
    assert out.auto_approved is True and out.aa_grant["basis"] == "archive: owner lead"


def test_a_target_that_turns_it_on_behaves_like_any_other(create):
    out, _insert = create["go"](ARCHIVE_ON, CAPTAIN)
    assert out.auto_approved is True and _granted(out) == 502
    create["prior"] = PRIOR
    out, _insert = create["go"](ARCHIVE_ON, MEMBER)
    assert out.auto_approved is True and out.fp_hit == PRIOR
    assert _actions(create) == ["submitted", "auto_approved_fingerprint"]
    create["prior"] = None
    for user in (ADMIN, LEAD_BARE):              # no role rule there
        out, _insert = create["go"](ARCHIVE_ON, user)
        assert out.auto_approved is False
    assert create["asked"] == []


@pytest.mark.parametrize("user,waiver", [(LEAD, 501), (NAMED, 503), (CAPTAIN, 502)])
def test_a_target_that_turns_it_off_lets_no_waiver_decide(create, user, waiver, caplog):
    with caplog.at_level(logging.INFO, logger="queryhub.core_submit"):
        out, _insert = create["go"](ARCHIVE_OFF, user)
    assert out.auto_approved is False
    assert out.aa_warn and "Auto-approve is off for this connection" in out.aa_warn
    assert any(f"waiver {waiver} not applied" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("user", [ADMIN, LEAD_BARE])
def test_nor_any_role(create, user):
    create["prior"] = PRIOR
    out, _insert = create["go"](ARCHIVE_OFF, user)
    assert out.auto_approved is False and out.aa_warn is None
    assert create["cache_asked"] == 0 and create["asked"] == []


@pytest.mark.parametrize("engine", OTHER_ENGINES)
def test_other_engines_still_honour_every_waiver(create, engine):
    out, _insert = create["go"](_target(engine), CAPTAIN)
    assert out.auto_approved is True and _granted(out) == 502
    assert out.aa_warn is None
    assert _actions(create) == ["submitted", "auto_approved"]


@pytest.mark.parametrize("engine", OTHER_ENGINES)
def test_other_engines_still_honour_the_cache(create, engine):
    create["prior"] = PRIOR
    out, _insert = create["go"](_target(engine), MEMBER)
    assert out.auto_approved is True and out.fp_hit == PRIOR
    assert create["cache_asked"] == 1


@pytest.mark.parametrize("engine", OTHER_ENGINES)
def test_on_other_engines_a_role_without_a_waiver_still_waits(create, engine):
    for user in (ADMIN, LEAD_BARE):
        out, _insert = create["go"](_target(engine), user)
        assert out.auto_approved is False and out.aa_warn is None
    assert create["asked"] == []


@pytest.mark.parametrize("target", [ARCHIVE, ARCHIVE_OFF, ARCHIVE_ON, LEDGER],
                         ids=["athena", "athena-off", "athena-on", "postgres"])
def test_a_super_admins_own_query_is_still_the_super_admin_rule(create, target):
    create["prior"] = PRIOR
    out, _insert = create["go"](target, SUPER)
    assert out.auto_approved is True
    assert out.aa_grant is None and out.fp_hit is None and create["cache_asked"] == 0
    assert _actions(create) == ["submitted", "auto_approved_super"]
    assert dict(create["audit"])["auto_approved_super"]["reason"] == "super-admin full access"


# ---------------------------------------------------------------------------
# what the admins are told
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("grant,label", [
    (WIDE[CAPTAIN], "via grant #502"),
    ({"id": None, "basis": "archive: admin", "max_tier": "ro"}, "via archive: admin"),
])
def test_the_admin_fyi_names_what_decided(monkeypatch, grant, label):
    from queryhub.slack_app import notifications
    sent = []
    monkeypatch.setattr(notifications, "deliver_auto_approve_fyi",
                        lambda client, request, header, quiet: sent.append((header, quiet)))
    notifications.dm_admins_auto_approved(
        None, {"id": 42, "requester_slack_id": MEMBER, "database_name": "ledger"},
        ARCHIVE, grant)
    [(header, quiet)] = sent
    assert f"(RO {label})" in header and quiet is True


def test_the_requester_is_told_the_basis(monkeypatch):
    from queryhub.slack_app import notifications
    told, ran = [], []
    monkeypatch.setattr(notifications, "dm_admins_auto_approved", lambda *a: None)
    monkeypatch.setattr(notifications, "dm_requester",
                        lambda client, uid, text=None, blocks=None: told.append(text))
    monkeypatch.setattr(notifications, "request_context_with_query_md", lambda row: "")
    monkeypatch.setattr(executor, "submit", lambda row, client: ran.append(row["id"]))
    basis = {"id": None, "basis": "archive: owner lead", "max_tier": "ro"}
    outcome = cs.Outcome(row={"id": 42}, auto_approved=True, aa_grant=basis)
    assert cs.dispatch_and_notify(None, _prepared(ARCHIVE, LEAD_BARE), outcome) == "dispatched"
    assert "(archive: owner lead, max_tier=ro)" in told[0] and ran == [42]


# ---------------------------------------------------------------------------
# validate_submission: the justification exemption
# ---------------------------------------------------------------------------

def _patch_validate(monkeypatch, st):
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.requesters, "open_request_count", lambda uid: 0)
    monkeypatch.setattr(cs.admins, "is_super_admin", _super)
    monkeypatch.setattr(cs.targets, "get", lambda tid: st["target"])
    monkeypatch.setattr(cs.teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ro"})
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "ro")
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(cs.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(athena_exec, "config_of", lambda t: {})
    monkeypatch.setattr(athena_exec, "risk_hint", lambda *a, **k: None)
    monkeypatch.setattr(cs, "_archive_freshness", lambda t, c: None)
    monkeypatch.setattr(cs.cfg, "get_bool",
                        lambda key, default=False: key == "require_justification" or default)


def _validate(st, user):
    return cs.validate_submission(
        user, "Ex", target_server_id=st["target"].id, database_name="ledger",
        query=READ, justification=None)


@pytest.fixture
def validate(monkeypatch, world):
    """validate_submission with `require_justification` on, so a read without
    a reason passes only when something exempts it."""
    st = world
    st["target"] = ARCHIVE
    _patch_validate(monkeypatch, st)
    st["go"] = lambda user: _validate(st, user)
    return st


@pytest.mark.parametrize("user", [LEAD, LEAD_BARE, ADMIN, CAPTAIN, NAMED])
def test_whoever_skips_review_on_the_archive_is_excused_the_reason(validate, user):
    assert isinstance(validate["go"](user), cs.Prepared)


def test_a_member_is_asked_for_one(validate):
    """It reaches an approver, who is the reader the field is for."""
    out = validate["go"](MEMBER)
    assert isinstance(out, cs.Rejection) and out.field == "justification"


@pytest.mark.parametrize("user", [LEAD, ADMIN, NAMED])
def test_nobody_is_excused_where_the_target_turns_it_off(validate, user):
    validate["target"] = ARCHIVE_OFF
    out = validate["go"](user)
    assert isinstance(out, cs.Rejection) and out.field == "justification"


@pytest.mark.parametrize("target", [LEDGER, ARCHIVE_ON], ids=["postgres", "athena-on"])
def test_a_waiver_still_excuses_it_where_every_waiver_decides(validate, target):
    validate["target"] = target
    assert isinstance(validate["go"](CAPTAIN), cs.Prepared)
    assert isinstance(validate["go"](ADMIN), cs.Rejection), "no role rule there"


@pytest.mark.parametrize("target", [ARCHIVE, ARCHIVE_OFF])
def test_a_super_admin_is_excused_on_an_archive_too(validate, target):
    validate["target"] = target
    assert isinstance(validate["go"](SUPER), cs.Prepared)


# ---------------------------------------------------------------------------
# the two batch paths
# ---------------------------------------------------------------------------

class _BatchCur:
    """The bundle transaction: an auto-approval UPDATE returns its new state."""

    def __init__(self):
        self.last = ""

    def execute(self, sql, params=None):
        self.last = " ".join(sql.split())

    def fetchone(self):
        return {"status": "approved"} if self.last.startswith("UPDATE requests") else None


def _bundle(cur, **kw):
    return {"bundle_id": 5,
            "item_rows": [{"id": 11 + i, "position": i + 1, "status": "pending"}
                          for i in range(len(kw["items"]))]}


@pytest.fixture
def batch_common(monkeypatch, world):
    st = world
    st.update({"audit": [], "dispatched": []})

    @contextlib.contextmanager
    def txn():
        yield _BatchCur()
    monkeypatch.setattr(cs.db, "transaction", txn)
    monkeypatch.setattr(bundles, "is_enabled", lambda: True)
    monkeypatch.setattr(bundles, "max_items", lambda: 5)
    monkeypatch.setattr(bundles, "insert_bundle_with_items", _bundle)
    monkeypatch.setattr(executor, "submit",
                        lambda row, client: st["dispatched"].append(row["id"]))
    return st


# Item 11 is on LEDGER, item 12 on ARCHIVE. The admin shows the role half is
# the archive's only: their ledger read waits, their archive read runs.
BATCH_CASES = [(MEMBER, []), (CAPTAIN, [11, 12]), (LEAD, [11, 12]), (ADMIN, [12]),
               (LEAD_BARE, [12])]


def _decided(st, rid):
    return next((d for r, action, d in st["audit"] if r == rid and action == "auto_approved"),
                None)


@pytest.mark.parametrize("user,runs", BATCH_CASES)
def test_a_slack_batch_decides_its_archive_item_by_the_same_rule(batch_common, monkeypatch,
                                                                 user, runs):
    from queryhub.slack_app import handlers
    st = batch_common
    st.update(notified=[], dms=[])
    by_id = {LEDGER.id: LEDGER, ARCHIVE.id: ARCHIVE}
    raw = [{"target_server_id": LEDGER.id}, {"target_server_id": ARCHIVE.id}]

    def validated(*, user_id, raw_item, bundle_justification):
        t = by_id[raw_item["target_server_id"]]
        return ({"target_server_id": t.id, "target_alias": t.alias,
                 "database_name": "ledger", "query": READ, "wants_result": True,
                 "result_format": "csv", "required_mode": "ro",
                 "explain_plan": None}, {})
    monkeypatch.setattr(handlers, "_ack_logging", lambda ack, body, mode: ack)
    monkeypatch.setattr(handlers, "_kill_switch_on", lambda: False)
    monkeypatch.setattr(handlers.modal, "parse_batch_submission",
                        lambda view: {"items": raw, "schedule_date": None,
                                      "schedule_time": None, "justification": None})
    monkeypatch.setattr(handlers, "_resolve_schedule", lambda d, t, uid: (None, None, None))
    monkeypatch.setattr(handlers.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(handlers, "_validate_batch_item", validated)
    monkeypatch.setattr(handlers.targets, "get", lambda tid: by_id.get(tid))
    monkeypatch.setattr(handlers.audit, "log_in",
                        lambda cur, rid, actor, name, action, details=None:
                        st["audit"].append((rid, action, details)))
    monkeypatch.setattr(handlers.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(handlers, "_dm_admins_bundle_auto_approved", lambda *a: None)
    monkeypatch.setattr(handlers.admins, "list_active", lambda: [{"slack_user_id": "U0EXAMPLE009"}])
    monkeypatch.setattr(handlers.notifications, "notify_admins_bundle",
                        lambda client, bid: st["notified"].append(bid))
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda client, uid, text=None, blocks=None: st["dms"].append(text))

    handlers.handle_batch_submission(lambda *a, **k: None,
                                     {"user": {"id": user, "name": "Ex"}, "view": {}}, None)

    assert st["dispatched"] == runs
    submitted = {rid: d for rid, action, d in st["audit"] if action == "submitted"}
    assert [rid for rid in (11, 12) if submitted[rid]["auto_approved"]] == runs
    assert [rid for rid, action, _d in st["audit"] if action == "auto_approved"] == runs
    assert st["notified"] == ([] if len(runs) == 2 else [5]), "a pending item goes to the approvers"
    if user in (ADMIN, LEAD_BARE):
        assert "grant_id" not in _decided(st, 12)
        assert _decided(st, 12)["basis"].startswith("archive: ")


@pytest.mark.parametrize("user,runs", BATCH_CASES)
def test_a_web_batch_decides_its_archive_item_by_the_same_rule(batch_common, monkeypatch,
                                                               user, runs):
    from queryhub.web import routes_queries as rq
    st = batch_common
    by_alias = {LEDGER.alias: LEDGER, ARCHIVE.alias: ARCHIVE}

    def validated(uid, name, *, target_server_id, database_name, query, **kw):
        t = next(t for t in by_alias.values() if t.id == target_server_id)
        return _prepared(t, uid)
    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq.deps, "block_if_password_change_required", lambda claims: None)
    monkeypatch.setattr(rq, "_client_ctx", lambda request: (None, None))
    monkeypatch.setattr(rq, "_target_by_alias", lambda alias: by_alias.get(alias))
    monkeypatch.setattr(teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ro"})
    monkeypatch.setattr(rq.core_submit, "validate_submission", validated)
    monkeypatch.setattr(rq.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(rq.audit_mod, "log_in",
                        lambda cur, rid, actor, name, action, details=None:
                        st["audit"].append((rid, action, details)))
    monkeypatch.setattr(rq, "_bot_client", lambda: None)
    monkeypatch.setattr(rq.admins, "list_active", lambda: [])

    body = rq.BatchIn(items=[rq.BatchItemIn(connectionId=LEDGER.alias, sql=READ),
                             rq.BatchItemIn(connectionId=ARCHIVE.alias, sql=READ)])
    out = rq.submit_batch(body, request=None, claims={"sub": user, "name": "Ex"})

    assert st["dispatched"] == runs
    status = {item["queryId"]: item["status"] for item in out["items"]}
    for rid in (11, 12):
        assert status[str(rid)] == rq.mapping.status_to_web(
            "approved" if rid in runs else "pending")
    assert [rid for rid, action, _d in st["audit"] if action == "auto_approved"] == runs
    if user in (ADMIN, LEAD_BARE):
        assert _decided(st, 12) == {"basis": auto_approve.ARCHIVE_BASIS[
            "admin" if user == ADMIN else "owner lead"], "max_tier": "ro"}


# ---------------------------------------------------------------------------
# every announcement agrees with create_request
# ---------------------------------------------------------------------------

@pytest.fixture
def surfaces(monkeypatch, world):
    """The four answers for one (target, person): the request create_request
    makes, the justification exemption, /classify, and /connections."""
    from queryhub.web import routes_data as rd, routes_queries as rq
    st = world
    st.update({"target": ARCHIVE, "prior": None, "cache_asked": 0, "sql": [], "audit": []})
    _patch_create(monkeypatch, st)
    _patch_validate(monkeypatch, st)
    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq, "_target_by_alias", lambda alias: st["target"])
    monkeypatch.setattr(rd.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rd.targets, "list_enabled", lambda: [st["target"]])
    monkeypatch.setattr(rd.teams, "effective_grants_for_user",
                        lambda uid, ids: {i: {"allowed_databases": None, "mode": "ro"} for i in ids})
    monkeypatch.setattr(rd, "_catalog_databases_map", lambda ids: {i: ["ledger"] for i in ids})
    monkeypatch.setattr(rd, "_catalog_table_refs_map", lambda pairs: {})
    monkeypatch.setattr(rd, "_catalog_functions_map", lambda pairs: {})

    def answers(target, user):
        st["target"] = target
        st["sql"].clear()
        created = cs.create_request(_prepared(target, user)).auto_approved
        exempt = isinstance(_validate(st, user), cs.Prepared)
        body = rq.ClassifyIn(connectionId=target.alias, databaseId="ledger", sql=READ)
        classified = rq.classify_query(body, claims={"sub": user})["willAutoApprove"]
        [conn] = rd.connections(claims={"sub": user})["connections"]
        listed = conn["databases"][0]["autoApproveRO"]
        assert conn["autoApproveRO"] is listed
        return created, exempt, classified, listed
    st["answers"] = answers
    return st


AGREE_TARGETS = [ARCHIVE, ARCHIVE_ON, ARCHIVE_OFF, LEDGER, _target("mssql"),
                 _target("clickhouse")]
# The rule, written out for the archive itself: who skips review there.
ON_THE_ARCHIVE = {LEAD: True, LEAD_BARE: True, ADMIN: True, CAPTAIN: True, NAMED: True,
                  BOTH: True, MEMBER: False}


@pytest.mark.parametrize("target", AGREE_TARGETS,
                         ids=["athena", "athena-on", "athena-off", *OTHER_ENGINES])
@pytest.mark.parametrize("user", list(ON_THE_ARCHIVE))
def test_every_announcement_agrees_with_the_decision(surfaces, target, user):
    """A super-admin is left out on purpose: their own submission is the
    super-admin rule, which /connections has never announced."""
    created, exempt, classified, listed = surfaces["answers"](target, user)
    assert (exempt, classified, listed) == (created, created, created)
    if target is ARCHIVE:
        assert created is ON_THE_ARCHIVE[user]
    if target is ARCHIVE_OFF:
        assert created is False


# ---------------------------------------------------------------------------
# what the web says before anything is submitted
# ---------------------------------------------------------------------------

@pytest.fixture
def classify(monkeypatch, world):
    from queryhub.web import routes_queries as rq
    st = world
    st.update({"target": ARCHIVE})
    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq, "_target_by_alias", lambda alias: st["target"])
    monkeypatch.setattr(rq.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(rq.admins, "is_super_admin", _super)
    monkeypatch.setattr(teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ro"})
    monkeypatch.setattr(teams, "effective_mode_for_database", lambda uid, tid, d: "ro")

    def go(user):
        body = rq.ClassifyIn(connectionId=st["target"].alias, databaseId="ledger", sql=READ)
        return rq.classify_query(body, claims={"sub": user})
    st["go"] = go
    return st


@pytest.mark.parametrize("user,expected", list(ON_THE_ARCHIVE.items()))
def test_the_editor_says_auto_approve_on_the_archive_where_it_applies(classify, user,
                                                                      expected):
    got = classify["go"](user)
    assert got["tier"] == "RO" and got["blocked"] is False
    assert got["willAutoApprove"] is expected


def test_the_editor_says_nothing_applies_where_the_target_turns_it_off(classify):
    classify["target"] = ARCHIVE_OFF
    for user in (LEAD, ADMIN, CAPTAIN):
        assert classify["go"](user)["willAutoApprove"] is False


@pytest.mark.parametrize("target", [ARCHIVE, ARCHIVE_OFF])
def test_a_super_admin_is_told_their_archive_query_runs(classify, target):
    classify["target"] = target
    assert classify["go"](SUPER)["willAutoApprove"] is True


# ---------------------------------------------------------------------------
# the Slack modal's badge and the read-burst nudge
# ---------------------------------------------------------------------------

@pytest.fixture
def banner(monkeypatch, world):
    from queryhub.slack_app import handlers, modal
    st = world
    st.update({"burst": None, "fleet": [LEDGER, ARCHIVE],
               "reach": {LEDGER.id, ARCHIVE.id}, "dms": []})
    monkeypatch.setattr(modal, "_recent_ro_burst", lambda pid: st["burst"])
    monkeypatch.setattr(modal.targets, "get",
                        lambda tid: next((t for t in st["fleet"] if t.id == tid), None))
    monkeypatch.setattr(modal.targets, "list_enabled", lambda: st["fleet"])
    monkeypatch.setattr(modal.teams, "can_use_target", lambda pid, tid: tid in st["reach"])
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda client, pid, text=None, blocks=None: st["dms"].append(blocks))

    def text(user):
        return json.dumps(modal._auto_approve_banner(user), ensure_ascii=False)
    st["text"], st["handlers"] = text, handlers
    return st


def _burst(target):
    return {"count": 3, "target_server_id": target.id, "database_name": "ledger"}


@pytest.mark.parametrize("user", [CAPTAIN, LEAD, MEMBER, ADMIN])
def test_an_archive_burst_is_not_offered_a_window(banner, user):
    """No window is offered on the archive, whoever bursts there."""
    banner["burst"] = _burst(ARCHIVE)
    text = banner["text"](user)
    assert "Running a lot of reads?" not in text
    assert '"text": "Request"' in text, "the modest button stays for elsewhere"


def test_a_burst_elsewhere_still_is(banner):
    banner["burst"] = _burst(LEDGER)
    assert "Running a lot of reads?" in banner["text"](MEMBER)


@pytest.mark.parametrize("user", [CAPTAIN, LEAD, ADMIN])
def test_an_archive_burst_gets_no_dm_and_one_elsewhere_does(banner, user):
    banner["held"][user] = []                 # nothing covers the burst anywhere
    banner["burst"] = _burst(ARCHIVE)
    banner["handlers"]._maybe_dm_ro_burst(None, user, "ro")
    assert banner["dms"] == []
    banner["burst"] = _burst(LEDGER)
    banner["handlers"]._maybe_dm_ro_burst(None, user, "ro")
    assert len(banner["dms"]) == 1


@pytest.mark.parametrize("user", [CAPTAIN, LEAD])
def test_an_every_connection_badge_reaches_the_archive(banner, user):
    text = banner["text"](user)
    assert "on every connection (up to *RO*" in text and "except" not in text


@pytest.mark.parametrize("user,phrase", [(ADMIN, "as an admin"),
                                         (LEAD_BARE, "as the owning team's lead")])
def test_a_role_on_the_archive_is_badged(banner, user, phrase):
    text = banner["text"](user)
    assert f"Auto-approve active* on `example-athena` (all dbs) (up to *RO*, {phrase})" in text
    assert "example-postgres" not in text, "the role is the archive's only"


def test_a_role_already_covered_by_a_waiver_says_nothing_more(banner):
    banner["held"][ADMIN] = [_waiver(511, ADMIN)]
    text = banner["text"](ADMIN)
    assert "on every connection (up to *RO*" in text and "as an admin" not in text


def test_a_member_is_promised_nothing(banner):
    assert "Auto-approve active" not in banner["text"](MEMBER)


def test_an_archive_the_reader_cannot_query_is_not_named(banner):
    banner["reach"] = {LEDGER.id}
    assert "Auto-approve active" not in banner["text"](ADMIN)
    text = banner["text"](CAPTAIN)
    assert "on every connection (up to *RO*" in text and "example-athena" not in text


def test_a_waiver_that_names_the_archive_is_badged(banner):
    assert "Auto-approve active* on `example-athena` (all dbs)" in banner["text"](NAMED)


def test_an_archive_that_turns_it_off_badges_nothing_there(banner):
    banner["fleet"] = [LEDGER, ARCHIVE_OFF]
    assert "Auto-approve active" not in banner["text"](NAMED)
    assert "Auto-approve active" not in banner["text"](ADMIN)
    assert "every connection except `example-athena`" in banner["text"](CAPTAIN)


def test_an_archive_that_turns_it_on_is_badged_like_any_other(banner):
    banner["fleet"] = [LEDGER, ARCHIVE_ON]
    assert "Auto-approve active* on `example-athena` (all dbs)" in banner["text"](NAMED)
    assert "on every connection (up to *RO*" in banner["text"](CAPTAIN)
    assert "Auto-approve active" not in banner["text"](ADMIN), "no role rule there"


# ---------------------------------------------------------------------------
# asking for a window
# ---------------------------------------------------------------------------

@pytest.fixture
def window(monkeypatch):
    from queryhub import auto_approve_requests as aar
    st = {"target": ARCHIVE, "made": [], "aar": aar}
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(teams, "can_use_target", lambda pid, tid: True)
    monkeypatch.setattr(aar, "find_pending_for", lambda pid, tid: None)
    monkeypatch.setattr(targets, "get", lambda tid: st["target"])
    monkeypatch.setattr(aar, "create", lambda **kw: st["made"].append(kw) or {"id": 7})

    def go(user):
        return aar.submit_window(principal_id=user, name="Ex", target_id=st["target"].id,
                                 window_minutes=60, reason="reading the archive")
    st["go"] = go
    return st


@pytest.mark.parametrize("user", [MEMBER, CAPTAIN, LEAD, ADMIN])
def test_a_window_on_an_archive_is_still_refused(window, user):
    """Approved, it would be a waiver naming the archive, which applies: a
    member's reads would skip the lead's review. The lead and the admins need
    none."""
    with pytest.raises(window["aar"].WindowRequestRefused) as e:
        window["go"](user)
    assert (e.value.field, e.value.status) == ("target", 409)
    assert "not offered on this connection" in e.value.message
    assert window["made"] == []


def test_a_window_on_an_archive_that_turns_it_on_is_filed(window):
    window["target"] = ARCHIVE_ON
    row, _t = window["go"](MEMBER)
    assert row == {"id": 7} and len(window["made"]) == 1


# ---------------------------------------------------------------------------
# writing a waiver: "already covered" only by one that applies
# ---------------------------------------------------------------------------

@pytest.fixture
def plans(monkeypatch, world):
    world["by_id"] = {LEDGER.id: LEDGER, ARCHIVE.id: ARCHIVE}
    monkeypatch.setattr(targets, "get", lambda tid: world["by_id"].get(tid))
    return world


@pytest.mark.parametrize("archive,target_id,covered", [
    (ARCHIVE, ARCHIVE.id, 502),       # a fleet-wide waiver applies on the archive now
    (ARCHIVE, LEDGER.id, 502),
    (ARCHIVE_OFF, ARCHIVE.id, None),  # it decides nothing there
], ids=["archive", "postgres", "archive-off"])
def test_a_waiver_naming_the_archive_is_covered_only_where_one_applies(plans, archive,
                                                                       target_id, covered):
    plans["by_id"][ARCHIVE.id] = archive
    [item] = grants._waiver_plan(CAPTAIN, "ro", target_id, None, None)
    assert (item["covered_by"] or {}).get("id") == covered


@pytest.mark.parametrize("archive,target_id,covered", [
    (ARCHIVE, ARCHIVE.id, "ag:31"), (ARCHIVE, LEDGER.id, "ag:31"), (ARCHIVE_OFF, ARCHIVE.id, None),
], ids=["archive", "postgres", "archive-off"])
def test_a_teams_fleet_wide_waiver_covers_the_archive_unless_it_is_off(plans, monkeypatch,
                                                                      archive, target_id,
                                                                      covered):
    from queryhub.web import routes_admin as ra
    plans["by_id"][ARCHIVE.id] = archive
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: True)
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: [
        {"id": 31, "tier": "ro", "target_id": None, "database_name": None,
         "valid_from": NOW - timedelta(days=1), "valid_until": None}])
    [item] = ra._team_waiver_plan({"id": 17}, target_id, None, "ro", None)
    assert (item["covered_by"] or {}).get("id") == covered


# ---------------------------------------------------------------------------
# the audit trail says how a machine acted
# ---------------------------------------------------------------------------

def test_the_audit_trail_names_the_archive_basis():
    from queryhub.web import routes_admin as ra
    row = {"details": {"basis": "archive: admin", "max_tier": "ro"}}
    assert ra._audit_via(row, {}) == "auto-approve rule · archive: admin"
    assert ra._audit_via({"details": {"max_tier": "ro"}}, {}) is None


# ---------------------------------------------------------------------------
# the next caller
# ---------------------------------------------------------------------------

PACKAGE = Path(cs.__file__).resolve().parent
# Every caller of the one question there is today. Listed so the scan cannot
# pass by finding nothing; a new caller does not need adding here, only to ask
# `decision` rather than `effective_grant`.
KNOWN = {
    "core_submit.validate_submission",
    "core_submit.create_request",
    "slack_app.handlers._maybe_dm_ro_burst",
    "slack_app.handlers.handle_batch_submission",
    "slack_app.modal._auto_approve_banner",
    "slack_app.modal._archive_role_scopes",
    "web.routes_data.connections",
    "web.routes_queries.classify_query",
    "web.routes_queries.submit_batch",
}


def _name(func) -> str:
    return (func.attr if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name) else "")


def _calls(node) -> set[str]:
    return {_name(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}


def _modules():
    for path in sorted(PACKAGE.rglob("*.py")):
        module = ".".join(path.relative_to(PACKAGE).with_suffix("").parts)
        yield module, ast.parse(path.read_text(encoding="utf-8"))


def _functions():
    for module, tree in _modules():
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"{module}.{fn.name}", fn


def test_every_auto_approve_question_goes_through_decision():
    """A waiver looked up without `decision` skips the archive rule's role
    half and its RO-only line, and either side of it -- the decision or a
    promise -- would disagree with create_request. Read from the code, so the
    caller added next fails here rather than in production."""
    callers = {name for name, fn in _functions() if "decision" in _calls(fn)}
    assert KNOWN <= callers, f"the scan missed {sorted(KNOWN - callers)}"
    around = sorted(name for name, fn in _functions()
                    if "effective_grant" in _calls(fn) and name != "auto_approve.decision")
    assert not around, (f"{around} call effective_grant directly; ask "
                        "auto_approve.decision(principal, mode, target, database)")


def test_the_one_question_filters_every_waiver():
    """Per call: the lookup inside `decision` passes `applies=`, so a waiver
    the target turns away is never the answer."""
    unfiltered = []
    for module, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) == "effective_grant":
                applies = next((k.value for k in node.keywords if k.arg == "applies"), None)
                if applies is None or (isinstance(applies, ast.Constant)
                                       and applies.value is None):
                    unfiltered.append(f"{module}:{node.lineno}")
    assert not unfiltered, f"effective_grant called without applies= at {unfiltered}"


def test_the_fingerprint_cache_asks_whether_everything_may_skip_review():
    """The cache never applies on an archive: every caller asks
    `engines.auto_approve_allowed` first."""
    callers = {name: _calls(fn) for name, fn in _functions()
               if name != "auto_approve.fingerprint_cache_hit"
               and "fingerprint_cache_hit" in _calls(fn)}
    assert "core_submit.create_request" in callers
    ungated = sorted(n for n, calls in callers.items() if "auto_approve_allowed" not in calls)
    assert not ungated, f"{ungated} consult the fingerprint cache without auto_approve_allowed"


def test_the_role_half_is_reached_only_through_decision():
    callers = sorted(name for name, fn in _functions()
                     if {"archive_role", "_archive_role_basis"} & _calls(fn))
    assert callers == ["auto_approve._archive_role_basis", "auto_approve.decision"], callers
