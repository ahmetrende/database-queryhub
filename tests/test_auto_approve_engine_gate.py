"""On an Athena archive only two waivers apply, and the fingerprint cache never.

Operator decision C3 (2026-09-20): a query on an Athena archive is reviewed by a
person. When an archive target opened with a team read grant, nothing enforced
that: a pod captain on the team held a fleet-wide read waiver with no expiry,
and `effective_grant` returns such a waiver for an archive exactly as for any
other target, so their archive queries would have run with no review. The
fingerprint cache did the same for every member's repeat query shapes, and on
Athena the literal values a fingerprint ignores are the partitions a query
scans, which is what it costs.

The operator's rule (2026-10-01) refines it: nobody holds more than RO on the
archive, the lead of the team that owns it has auto-approve, and everyone
else's query goes to the lead and an admin. So, unless the target's
`engine_config.auto_approve` is a JSON boolean:

1. a waiver that names the archive applies as usual;
2. a fleet-wide waiver applies only for the owning team's lead, which the
   access model records as an approver role scoped to the archive
   (`access.approves_target`); somebody who reaches every target does not
   qualify by holding one;
3. the fingerprint cache never applies;
4. `engine_config.auto_approve: true` turns everything on, `false` everything
   off, rules 1 and 2 included;
5. a super-admin's own submission is unchanged.

`auto_approve.waiver_applies` is the one answer for a waiver, passed to
`effective_grant` as `applies`; `engines.auto_approve_allowed` stays the answer
for the cache and for offers made with no waiver in hand. The last tests here
read the package and fail on a caller that skips either question.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from queryhub import (access, athena_exec, auto_approve, bundles, core_submit as cs, engines,
                           executor, grants, lifecycle, targets, teams)

LEAD = "U0EXAMPLE001"     # leads the team that owns the archive
BYPASS = "U0EXAMPLE002"   # reaches every target; no role on the archive
MEMBER = "U0EXAMPLE003"   # on the owning team; holds no waiver
NAMED = "U0EXAMPLE004"    # holds a waiver that names the archive
BOTH = "U0EXAMPLE005"     # a fleet-wide waiver AND one that names the archive
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


def _waiver(wid, holder, target_id=None, expires_at=None, team_id=None):
    row = {"id": wid, "slack_user_id": holder, "max_tier": "ro",
           "target_server_id": target_id, "database_name": None,
           "starts_at": NOW - timedelta(days=1), "expires_at": expires_at,
           "reason": "example", "granted_by": "U0EXAMPLE009"}
    if team_id is not None:
        row.update(team_id=team_id, team_name="example team")
    return row


# A fleet-wide read waiver with no expiry (target and database NULL), the shape
# a pod captain and a fleet-wide reader both hold.
WIDE = {who: _waiver(wid, who) for wid, who in ((501, LEAD), (502, BYPASS), (504, BOTH))}
ON_ARCHIVE = {NAMED: _waiver(503, NAMED, ARCHIVE.id),
              BOTH: _waiver(505, BOTH, ARCHIVE.id, expires_at=NOW + timedelta(days=1))}
HELD = {LEAD: [WIDE[LEAD]], BYPASS: [WIDE[BYPASS]], MEMBER: [],
        NAMED: [ON_ARCHIVE[NAMED]], BOTH: [ON_ARCHIVE[BOTH], WIDE[BOTH]]}
PRIOR = {"id": 77, "completed_at": NOW}


@pytest.fixture
def world(monkeypatch):
    """Who holds which waiver, and who leads the team that owns the archive.

    The lookups are faked at the bottom -- the live grant rows and the approver
    role -- so the real `effective_grant` and `waiver_applies` decide. The role
    is faked with raising=False so this fixture also runs against a tree that
    lacks the helper, and the tests below fail on their assertions there.
    """
    st = {"held": {k: list(v) for k, v in HELD.items()},
          "approves": {(LEAD, ARCHIVE.id)}, "asked": []}

    def active(pid, at=None):
        at = at or datetime.now(timezone.utc)
        return [w for w in st["held"].get(pid, [])
                if w["starts_at"] <= at and (w["expires_at"] is None or w["expires_at"] > at)]

    def approves(pid, tid):
        st["asked"].append((pid, tid))
        return (pid, tid) in st["approves"]
    monkeypatch.setattr(auto_approve, "active_grants", active)
    monkeypatch.setattr(access, "approves_target", approves, raising=False)
    return st


# ---------------------------------------------------------------------------
# the target's own answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("engine,expected", [
    ("postgres", True), ("mssql", True), ("clickhouse", True), ("athena", False),
])
def test_each_engine_has_a_default_and_only_athena_is_off(engine, expected):
    assert engines.spec(engine).auto_approve_default is expected
    assert engines.auto_approve_allowed(_target(engine)) is expected


def test_a_target_overrides_its_engine_in_both_directions():
    assert engines.auto_approve_allowed(ARCHIVE_ON) is True
    assert engines.auto_approve_allowed(_target("postgres", {"auto_approve": False})) is False
    assert engines.auto_approve_override(ARCHIVE_ON) is True
    assert engines.auto_approve_override(ARCHIVE_OFF) is False
    assert engines.auto_approve_override(ARCHIVE) is None


@pytest.mark.parametrize("value", ["true", "false", "yes", 1, 0, None, [], {}])
def test_only_a_json_boolean_is_an_answer(value):
    """A string that reads as true, or a 1, is not a decision anybody made: it
    falls back to the engine's default instead of opening the door."""
    assert engines.auto_approve_override(_target("athena", {"auto_approve": value})) is None
    assert engines.auto_approve_allowed(_target("athena", {"auto_approve": value})) is False
    assert engines.auto_approve_allowed(_target("postgres", {"auto_approve": value})) is True


def test_no_target_means_no_exemption():
    assert engines.auto_approve_allowed(None) is False


def test_an_engine_config_that_is_not_an_object_reads_as_the_default():
    class Odd:
        engine, engine_config = "athena", ["auto_approve", True]
    assert engines.auto_approve_allowed(Odd()) is False


# ---------------------------------------------------------------------------
# which waiver applies: rules 1, 2 and 4
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("holder,waiver,expected", [
    (LEAD, WIDE[LEAD], True),                          # rule 2: the owning team's lead
    (BYPASS, WIDE[BYPASS], False),                     # reaching every target is not it
    (NAMED, ON_ARCHIVE[NAMED], True),                  # rule 1
    (NAMED, _waiver(506, NAMED, LEDGER.id), False),    # names another target
    (None, WIDE[LEAD], False),                         # nobody to ask about
], ids=["lead", "bypass", "named", "named-elsewhere", "no-holder"])
def test_on_an_archive_two_waivers_apply(world, holder, waiver, expected):
    assert auto_approve.waiver_applies(ARCHIVE, waiver, holder) is expected


def test_only_a_fleet_wide_waiver_asks_who_leads_the_team(world):
    auto_approve.waiver_applies(ARCHIVE, ON_ARCHIVE[NAMED], NAMED)
    assert world["asked"] == []
    auto_approve.waiver_applies(ARCHIVE, WIDE[BYPASS], BYPASS)
    assert world["asked"] == [(BYPASS, ARCHIVE.id)]


def test_a_team_waiver_follows_the_same_rule(world):
    """A fleet-wide team waiver reaches the archive for the lead only."""
    team_wide = _waiver("ag:9", MEMBER, team_id=17)
    assert auto_approve.waiver_applies(ARCHIVE, team_wide, MEMBER) is False
    assert auto_approve.waiver_applies(ARCHIVE, {**team_wide, "slack_user_id": LEAD}, LEAD) is True


def test_a_target_that_turns_it_on_lets_every_waiver_apply(world):
    for holder in (LEAD, BYPASS, NAMED):
        for w in world["held"][holder]:
            assert auto_approve.waiver_applies(ARCHIVE_ON, w, holder) is True
    assert world["asked"] == []


def test_a_target_that_turns_it_off_lets_none_apply(world):
    """Rules 1 and 2 included: `false` is the target's own answer."""
    assert auto_approve.waiver_applies(ARCHIVE_OFF, WIDE[LEAD], LEAD) is False
    assert auto_approve.waiver_applies(ARCHIVE_OFF, ON_ARCHIVE[NAMED], NAMED) is False
    assert world["asked"] == []


@pytest.mark.parametrize("engine", ["postgres", "mssql", "clickhouse"])
def test_other_engines_let_every_waiver_apply(world, engine):
    t = _target(engine)
    assert auto_approve.waiver_applies(t, WIDE[BYPASS], BYPASS) is True
    assert auto_approve.waiver_applies(t, _waiver(507, NAMED, t.id), NAMED) is True
    assert world["asked"] == []


def test_no_target_or_no_waiver_means_no(world):
    assert auto_approve.waiver_applies(None, WIDE[LEAD], LEAD) is False
    assert auto_approve.waiver_applies(ARCHIVE, None, LEAD) is False
    assert auto_approve.waiver_applies(LEDGER, None, LEAD) is False


def test_the_lead_is_an_approver_role_that_names_the_target(monkeypatch):
    """`access.approves_target` reads the rows `roles` returns, which are
    already live, unrevoked and dropped for a disabled principal."""
    rows = {
        LEAD: [{"role": "approver", "scope_target_id": ARCHIVE.id, "all_targets": False}],
        BYPASS: [{"role": "admin", "scope_target_id": None, "all_targets": True},
                 {"role": "approver", "scope_target_id": None, "all_targets": True},
                 {"role": "granter", "scope_target_id": ARCHIVE.id, "all_targets": False}],
    }
    monkeypatch.setattr(access, "roles", lambda pid: rows.get(pid, []))
    assert access.approves_target(LEAD, ARCHIVE.id) is True
    assert access.approves_target(LEAD, LEDGER.id) is False
    assert access.approves_target(BYPASS, ARCHIVE.id) is False
    assert access.approves_target(MEMBER, ARCHIVE.id) is False


# ---------------------------------------------------------------------------
# effective_grant: the filter picks, it does not merely veto
# ---------------------------------------------------------------------------

def test_a_refused_row_is_passed_over_so_a_narrower_one_can_decide():
    """The masking case. The fleet-wide row has no expiry, so it sorts first;
    checking only the answer would refuse it and stop there."""
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
# create_request: the decision
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


@pytest.fixture
def create(monkeypatch, world):
    st = world
    st.update({"super": False, "prior": None, "cache_asked": 0, "sql": [], "audit": []})

    @contextlib.contextmanager
    def txn():
        yield _Cur(st)
    monkeypatch.setattr(cs.db, "transaction", txn)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: st["super"])

    def cache_hit(*a, **k):
        st["cache_asked"] += 1
        return st["prior"]
    monkeypatch.setattr(cs.auto_approve, "fingerprint_cache_hit", cache_hit)
    monkeypatch.setattr(cs.ast_safety, "fingerprint", lambda *a, **k: "fp-1")
    monkeypatch.setattr(cs.audit, "log_in",
                        lambda cur, rid, actor, name, action, details=None:
                        st["audit"].append((action, details)))

    def go(target, user, sched_for=None):
        st["sql"].clear()
        st["audit"].clear()
        prep = cs.Prepared(
            user_id=user, user_name="Ex", target=target, database="ledger",
            query=READ, required_mode="ro", justification=None, wants_result=True,
            result_format="csv", sched_for=sched_for, explain_plan=None,
            risk_summary=None, origin="web")
        out = cs.create_request(prep)
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


def test_the_owning_teams_lead_is_auto_approved_on_the_archive(create):
    out, (sql, _params) = create["go"](ARCHIVE, LEAD)
    assert out.auto_approved is True and _granted(out) == 501
    assert "decided_by_slack_id" in _columns(sql)
    assert _actions(create) == ["submitted", "auto_approved"]
    assert out.aa_warn is None and out.fp_hit is None


def test_a_fleet_wide_waiver_held_by_anyone_else_does_not_apply(create, caplog):
    with caplog.at_level(logging.INFO, logger="queryhub.core_submit"):
        out, (sql, _params) = create["go"](ARCHIVE, BYPASS)
    assert out.auto_approved is False and out.aa_grant is None
    assert "decided_by_slack_id" not in _columns(sql), "not the pending INSERT"
    assert _actions(create) == ["submitted"]
    assert "auto_approved" not in create["audit"][0][1]
    # The holder is told why; everywhere else their reads skip review.
    assert out.aa_warn and "Auto-approve is off for this connection" in out.aa_warn
    assert any("waiver 502 not applied" in r.getMessage() for r in caplog.records)


def test_a_member_with_no_waiver_waits_and_is_told_nothing(create):
    out, _insert = create["go"](ARCHIVE, MEMBER)
    assert out.auto_approved is False and out.aa_warn is None
    assert _actions(create) == ["submitted"]


def test_a_waiver_that_names_the_archive_applies(create):
    out, _insert = create["go"](ARCHIVE, NAMED)
    assert out.auto_approved is True and _granted(out) == 503
    assert _actions(create) == ["submitted", "auto_approved"]


def test_a_fleet_wide_waiver_does_not_mask_one_that_names_the_archive(create):
    out, _insert = create["go"](ARCHIVE, BOTH)
    assert out.auto_approved is True and _granted(out) == 505
    assert out.aa_warn is None


def test_a_scheduled_run_is_asked_the_same_question(create):
    """At the run time only the fleet-wide waiver is left, and on the archive it
    is not the lead's, so the run waits; the lead's still runs."""
    later = NOW + timedelta(days=2)
    out, _insert = create["go"](ARCHIVE, BOTH, sched_for=later)
    assert out.auto_approved is False
    assert out.aa_warn and "expires before the scheduled run time" in out.aa_warn
    out, _insert = create["go"](ARCHIVE, LEAD, sched_for=later)
    assert out.auto_approved is True and _granted(out) == 501


@pytest.mark.parametrize("user", [MEMBER, BYPASS])
def test_a_fingerprint_match_never_applies_on_the_archive(create, user):
    create["prior"] = PRIOR
    out, (_sql, params) = create["go"](ARCHIVE, user)
    assert out.auto_approved is False and out.fp_hit is None
    assert create["cache_asked"] == 0
    # Still computed and stored: the row reads like any other request's.
    assert "fp-1" in params


def test_the_lead_is_approved_by_the_waiver_not_by_the_cache(create):
    create["prior"] = PRIOR
    out, _insert = create["go"](ARCHIVE, LEAD)
    assert out.auto_approved is True and _granted(out) == 501
    assert out.fp_hit is None and create["cache_asked"] == 0


def test_the_target_can_turn_everything_on(create):
    out, _insert = create["go"](ARCHIVE_ON, BYPASS)
    assert out.auto_approved is True and _granted(out) == 502
    assert out.aa_warn is None
    create["prior"] = PRIOR
    out, _insert = create["go"](ARCHIVE_ON, MEMBER)
    assert out.auto_approved is True and out.fp_hit == PRIOR
    assert _actions(create) == ["submitted", "auto_approved_fingerprint"]


@pytest.mark.parametrize("user,waiver", [(LEAD, 501), (NAMED, 503)])
def test_the_target_can_turn_everything_off(create, user, waiver, caplog):
    with caplog.at_level(logging.INFO, logger="queryhub.core_submit"):
        out, _insert = create["go"](ARCHIVE_OFF, user)
    assert out.auto_approved is False
    assert out.aa_warn and "Auto-approve is off for this connection" in out.aa_warn
    assert any(f"waiver {waiver} not applied" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("engine", ["postgres", "mssql", "clickhouse"])
def test_other_engines_still_honour_every_waiver(create, engine):
    out, _insert = create["go"](_target(engine), BYPASS)
    assert out.auto_approved is True and _granted(out) == 502
    assert out.aa_warn is None
    assert _actions(create) == ["submitted", "auto_approved"]
    assert create["asked"] == []


@pytest.mark.parametrize("engine", ["postgres", "mssql", "clickhouse"])
def test_other_engines_still_honour_the_cache(create, engine):
    create["prior"] = PRIOR
    out, _insert = create["go"](_target(engine), MEMBER)
    assert out.auto_approved is True and out.fp_hit == PRIOR
    assert create["cache_asked"] == 1


@pytest.mark.parametrize("user", [BYPASS, MEMBER])
def test_a_super_admins_own_archive_query_still_auto_approves(create, user):
    create["super"] = True
    out, _insert = create["go"](ARCHIVE, user)
    assert out.auto_approved is True
    assert out.aa_grant is None and out.fp_hit is None
    details = dict(create["audit"])["auto_approved_super"]
    assert details["reason"] == "super-admin full access"


# ---------------------------------------------------------------------------
# validate_submission: the justification exemption
# ---------------------------------------------------------------------------

@pytest.fixture
def validate(monkeypatch, world):
    """validate_submission with `require_justification` on, so a read without
    a reason passes only when something exempts it."""
    st = world
    st.update({"super": False, "target": ARCHIVE})
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: st["super"])
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

    def go(user):
        return cs.validate_submission(
            user, "Ex", target_server_id=st["target"].id, database_name=None,
            query=READ, justification=None)
    st["go"] = go
    return st


def test_anyone_elses_waiver_does_not_excuse_the_reason_on_an_archive(validate):
    """It reaches an approver, who is the reader the field is for."""
    out = validate["go"](BYPASS)
    assert isinstance(out, cs.Rejection) and out.field == "justification"


@pytest.mark.parametrize("user", [LEAD, NAMED])
def test_a_waiver_that_applies_on_the_archive_excuses_it(validate, user):
    assert isinstance(validate["go"](user), cs.Prepared)


@pytest.mark.parametrize("target", [LEDGER, ARCHIVE_ON], ids=["postgres", "athena-on"])
def test_a_waiver_still_excuses_it_where_every_waiver_decides(validate, target):
    validate["target"] = target
    assert isinstance(validate["go"](BYPASS), cs.Prepared)


def test_a_super_admin_is_excused_on_an_archive_too(validate):
    validate["super"] = True
    assert isinstance(validate["go"](MEMBER), cs.Prepared)


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


@pytest.mark.parametrize("user,runs", [(BYPASS, [11]), (LEAD, [11, 12])])
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
    assert submitted[11]["auto_approved"] is True
    assert submitted[12]["auto_approved"] is (12 in runs)
    assert [rid for rid, action, _d in st["audit"] if action == "auto_approved"] == runs
    assert st["notified"] == ([] if 12 in runs else [5]), "a pending item goes to the approvers"


@pytest.mark.parametrize("user,runs", [(BYPASS, [11]), (LEAD, [11, 12])])
def test_a_web_batch_decides_its_archive_item_by_the_same_rule(batch_common, monkeypatch,
                                                               user, runs):
    from queryhub.web import routes_queries as rq
    st = batch_common
    by_alias = {LEDGER.alias: LEDGER, ARCHIVE.alias: ARCHIVE}

    def validated(uid, name, *, target_server_id, database_name, query, **kw):
        t = next(t for t in by_alias.values() if t.id == target_server_id)
        return cs.Prepared(
            user_id=uid, user_name=name, target=t, database="ledger", query=query,
            required_mode="ro", justification=None, wants_result=True,
            result_format="csv", sched_for=None, explain_plan=None,
            risk_summary=None, origin="web")
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
    assert status["11"] == rq.mapping.status_to_web("approved")
    assert status["12"] == rq.mapping.status_to_web("approved" if 12 in runs else "pending")
    assert [rid for rid, action, _d in st["audit"] if action == "auto_approved"] == runs


# ---------------------------------------------------------------------------
# what the web says before anything is submitted
# ---------------------------------------------------------------------------

@pytest.fixture
def classify(monkeypatch, world):
    from queryhub.web import routes_queries as rq
    st = world
    st.update({"target": ARCHIVE, "super": False})
    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq, "_target_by_alias", lambda alias: st["target"])
    monkeypatch.setattr(rq.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(rq.admins, "is_super_admin", lambda uid: st["super"])
    monkeypatch.setattr(teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ro"})
    monkeypatch.setattr(teams, "effective_mode_for_database", lambda uid, tid, d: "ro")

    def go(user):
        body = rq.ClassifyIn(connectionId=st["target"].alias, databaseId="ledger", sql=READ)
        return rq.classify_query(body, claims={"sub": user})
    st["go"] = go
    return st


@pytest.mark.parametrize("user,expected", [
    (LEAD, True), (NAMED, True), (BOTH, True), (BYPASS, False), (MEMBER, False),
])
def test_the_editor_says_auto_approve_on_the_archive_only_where_it_applies(classify, user,
                                                                           expected):
    got = classify["go"](user)
    assert got["tier"] == "RO" and got["blocked"] is False
    assert got["willAutoApprove"] is expected


@pytest.mark.parametrize("target", [LEDGER, ARCHIVE_ON], ids=["postgres", "athena-on"])
def test_the_editor_is_told_where_every_waiver_decides(classify, target):
    classify["target"] = target
    assert classify["go"](BYPASS)["willAutoApprove"] is True


def test_the_editor_says_nothing_applies_where_the_target_turns_it_off(classify):
    classify["target"] = ARCHIVE_OFF
    assert classify["go"](LEAD)["willAutoApprove"] is False


def test_a_super_admin_is_told_their_archive_query_runs(classify):
    classify["super"] = True
    assert classify["go"](MEMBER)["willAutoApprove"] is True


@pytest.mark.parametrize("user,archive", [(BYPASS, False), (LEAD, True), (NAMED, True),
                                          (MEMBER, False)])
def test_the_connection_list_promises_it_on_the_archive_only_where_it_applies(
        monkeypatch, world, user, archive):
    from queryhub.web import routes_data as rd
    fleet = [LEDGER, ARCHIVE,
             dataclasses.replace(ARCHIVE_ON, id=41, alias="example-archive-on")]
    monkeypatch.setattr(rd.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rd.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(rd.targets, "list_enabled", lambda: fleet)
    monkeypatch.setattr(rd.teams, "effective_grants_for_user",
                        lambda uid, ids: {i: {"allowed_databases": None, "mode": "ro"} for i in ids})
    monkeypatch.setattr(rd, "_catalog_databases_map", lambda ids: {i: ["ledger"] for i in ids})
    monkeypatch.setattr(rd, "_catalog_table_refs_map", lambda pairs: {})
    monkeypatch.setattr(rd, "_catalog_functions_map", lambda pairs: {})

    got = {c["id"]: c for c in rd.connections(claims={"sub": user})["connections"]}
    flags = {alias: (c["autoApproveRO"], [d["autoApproveRO"] for d in c["databases"]])
             for alias, c in got.items()}
    wide = user in (BYPASS, LEAD)          # holds a waiver that every other target honours
    assert flags == {"example-postgres": (wide, [wide]),
                     "example-athena": (archive, [archive]),
                     "example-archive-on": (wide, [wide])}


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


@pytest.mark.parametrize("user", [BYPASS, LEAD, MEMBER])
def test_an_archive_burst_is_not_offered_a_window(banner, user):
    """No window is offered on the archive, whoever bursts there."""
    banner["burst"] = _burst(ARCHIVE)
    text = banner["text"](user)
    assert "Running a lot of reads?" not in text
    assert '"text": "Request"' in text, "the modest button stays for elsewhere"


def test_a_burst_elsewhere_still_is(banner):
    banner["burst"] = _burst(LEDGER)
    assert "Running a lot of reads?" in banner["text"](MEMBER)


@pytest.mark.parametrize("user", [BYPASS, LEAD])
def test_an_archive_burst_gets_no_dm_and_one_elsewhere_does(banner, user):
    banner["held"][user] = []                 # nothing covers the burst anywhere
    banner["burst"] = _burst(ARCHIVE)
    banner["handlers"]._maybe_dm_ro_burst(None, user, "ro")
    assert banner["dms"] == []
    banner["burst"] = _burst(LEDGER)
    banner["handlers"]._maybe_dm_ro_burst(None, user, "ro")
    assert len(banner["dms"]) == 1


def test_an_every_connection_badge_names_the_archive_it_does_not_reach(banner):
    assert "on every connection except `example-athena` (up to *RO*" in banner["text"](BYPASS)


def test_the_leads_every_connection_badge_reaches_the_archive(banner):
    text = banner["text"](LEAD)
    assert "on every connection (up to *RO*" in text and "example-athena" not in text


def test_an_archive_the_reader_cannot_query_is_not_named(banner):
    banner["reach"] = {LEDGER.id}
    text = banner["text"](BYPASS)
    assert "on every connection (up to *RO*" in text and "example-athena" not in text


def test_a_waiver_that_names_the_archive_is_badged(banner):
    assert "Auto-approve active* on `example-athena` (all dbs)" in banner["text"](NAMED)


def test_a_waiver_naming_the_archive_is_not_swallowed_by_one_that_skips_it(banner):
    """The every-connection waiver does not reach the archive, so it cannot
    stand in for the one that does."""
    text = banner["text"](BOTH)
    assert "every connection except `example-athena`" in text
    assert "`example-athena` (all dbs)" in text


def test_an_archive_that_turns_it_off_badges_nothing_there(banner):
    banner["fleet"] = [LEDGER, ARCHIVE_OFF]
    assert "Auto-approve active" not in banner["text"](NAMED)
    assert "every connection except `example-athena`" in banner["text"](LEAD)


def test_an_archive_that_turns_it_on_is_badged_like_any_other(banner):
    banner["fleet"] = [LEDGER, ARCHIVE_ON]
    assert "Auto-approve active* on `example-athena` (all dbs)" in banner["text"](NAMED)
    assert "on every connection (up to *RO*" in banner["text"](BYPASS)


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


@pytest.mark.parametrize("user", [MEMBER, BYPASS, LEAD])
def test_a_window_on_an_archive_is_still_refused(window, user):
    """Approved, it would be a waiver naming the archive, which applies: a
    member's reads would skip the lead's review. The lead needs none."""
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
    by_id = {LEDGER.id: LEDGER, ARCHIVE.id: ARCHIVE}
    monkeypatch.setattr(targets, "get", lambda tid: by_id.get(tid))
    return world


@pytest.mark.parametrize("user,target,covered", [
    (BYPASS, ARCHIVE, None),     # the fleet-wide waiver does not apply there
    (BYPASS, LEDGER, 502),
    (LEAD, ARCHIVE, 501),        # the lead's does
])
def test_a_waiver_naming_the_archive_is_not_called_redundant(plans, user, target, covered):
    [item] = grants._waiver_plan(user, "ro", target.id, None, None)
    assert (item["covered_by"] or {}).get("id") == covered


@pytest.mark.parametrize("target,covered", [(ARCHIVE, None), (LEDGER, "ag:31")])
def test_a_teams_fleet_wide_waiver_does_not_cover_the_archive(plans, monkeypatch, target,
                                                              covered):
    from queryhub.web import routes_admin as ra
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: True)
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: [
        {"id": 31, "tier": "ro", "target_id": None, "database_name": None,
         "valid_from": NOW - timedelta(days=1), "valid_until": None}])
    [item] = ra._team_waiver_plan({"id": 17}, target.id, None, "ro", None)
    assert (item["covered_by"] or {}).get("id") == covered


# ---------------------------------------------------------------------------
# the next caller
# ---------------------------------------------------------------------------

PACKAGE = Path(cs.__file__).resolve().parent
# Each lookup, and the question a caller of it must ask.
GATES = {"effective_grant": "waiver_applies",
         "fingerprint_cache_hit": "auto_approve_allowed"}
# Every caller there is today. Listed so the scan cannot pass by finding
# nothing; a new caller does not need adding here, only to ask the question.
KNOWN = {
    "core_submit.validate_submission",
    "core_submit.create_request",
    "slack_app.handlers._maybe_dm_ro_burst",
    "slack_app.handlers.handle_batch_submission",
    "slack_app.modal._auto_approve_banner",
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
        if module == "auto_approve":
            continue                     # where the lookups and the answer live
        yield module, ast.parse(path.read_text(encoding="utf-8"))


def _callers() -> dict[str, set[str]]:
    found = {}
    for module, tree in _modules():
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = _calls(fn)
                if calls & set(GATES):
                    found[f"{module}.{fn.name}"] = calls
    return found


def test_every_caller_of_the_lookups_asks_its_question():
    """A waiver consulted without `waiver_applies`, or the fingerprint cache
    without `auto_approve_allowed`, is a decision or a promise the archive rule
    does not reach. Read from the code, so the caller added next fails here
    rather than in production."""
    callers = _callers()
    assert KNOWN <= set(callers), f"the scan missed {sorted(KNOWN - set(callers))}"
    ungated = sorted(f"{name} ({lookup} without {gate})"
                     for name, calls in callers.items()
                     for lookup, gate in GATES.items()
                     if lookup in calls and gate not in calls)
    assert not ungated, f"{ungated} skip the question that keeps the archive rule"


def test_every_effective_grant_call_passes_the_filter():
    """Per call, not per function: a second lookup in a function whose first
    one is filtered would decide unfiltered, and the function-level scan above
    would not see it."""
    unfiltered = []
    for module, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) == "effective_grant":
                applies = next((k.value for k in node.keywords if k.arg == "applies"), None)
                if applies is None or (isinstance(applies, ast.Constant)
                                       and applies.value is None):
                    unfiltered.append(f"{module}:{node.lineno}")
    assert not unfiltered, (
        f"effective_grant called without applies= at {unfiltered}; pass "
        "lambda g: auto_approve.waiver_applies(target, g, holder)")
