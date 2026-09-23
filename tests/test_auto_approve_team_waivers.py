"""A team's auto-approve waiver reaches the submit path.

Under the new access model a waiver can be granted to a TEAM, and a team has no
`auto_approve_grants` row to hold it: that table is keyed on one person. The
submit path decided auto-approval from that table alone, so a team waiver read
as "auto-approve" on every screen that asks the resolver while every member
still waited for review. Measured before the fix: 2 of 9,064 answers differed,
both team waivers, and nothing else.

Whether a team waiver applies to a given member is not decided here. It depends
on a rule `access._decide` owns -- a member's own grant on a database displaces
the team's rows there -- so the resolver is asked, and only when a team waiver
is the row that would decide.
"""
import inspect
from datetime import datetime, timezone

import pytest

from queryhub import access, auto_approve, teams

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
UID = "U0EXAMPLE001"


def _row(id_, tier="ro", target=53, db="ledger", team_id=None, team_name=None):
    return {"id": id_, "slack_user_id": UID, "max_tier": tier,
            "target_server_id": target, "database_name": db,
            "starts_at": NOW, "expires_at": None, "reason": None,
            "granted_by": None, "team_id": team_id, "team_name": team_name}


@pytest.fixture
def resolver(monkeypatch):
    """Stands in for `access.team_waivers_reach`, the one place rule 4 lives."""
    calls = []
    state = {"reach": True}

    def fake_reach(pid, tid, dbn):
        calls.append((pid, tid, dbn))
        return state["reach"]
    monkeypatch.setattr(access, "team_waivers_reach", fake_reach)
    state["calls"] = calls
    return state


# --- the decision ------------------------------------------------------------

def test_a_team_waiver_decides_when_it_reaches_the_member(resolver):
    team = _row("ag:461", team_id=14, team_name="Team Alpha")
    g = auto_approve.effective_grant(UID, "ro", 53, "ledger", rows=[team])
    assert g is team
    assert resolver["calls"] == [(UID, 53, "ledger")]


def test_a_team_waiver_is_refused_when_it_does_not_reach_the_member(resolver):
    """No access here, or the member's own grant displaces the team's rows."""
    resolver["reach"] = False
    team = _row("ag:461", team_id=14)
    assert auto_approve.effective_grant(UID, "ro", 53, "ledger", rows=[team]) is None


def test_a_personal_row_never_asks(resolver):
    own = _row(70)
    assert auto_approve.effective_grant(UID, "ro", 53, "ledger", rows=[own]) is own
    assert resolver["calls"] == []


def test_the_question_is_asked_at_most_once_per_decision(resolver):
    resolver["reach"] = False
    rows = [_row("ag:461", team_id=14), _row("ag:462", team_id=15)]
    assert auto_approve.effective_grant(UID, "ro", 53, "ledger", rows=rows) is None
    assert len(resolver["calls"]) == 1


def test_a_displaced_team_row_falls_through_to_the_members_own(resolver):
    """The case that would otherwise credit the team in the audit trail: the
    team row sorts first (it never expires), the rule says it does not reach
    this member, and their own waiver is the one that decides."""
    resolver["reach"] = False
    team = _row("ag:461", team_id=14)                     # no expiry: sorts first
    own = dict(_row(70), expires_at=datetime(2026, 11, 29, tzinfo=timezone.utc))
    g = auto_approve.effective_grant(UID, "ro", 53, "ledger", rows=[own, team])
    assert g is own
    assert auto_approve.decided_by_name_for(g).startswith("auto-approved (grant #70")


def test_a_team_waiver_below_the_request_tier_never_asks(resolver):
    team = _row("ag:461", tier="ro", team_id=14)
    assert auto_approve.effective_grant(UID, "rw", 53, "ledger", rows=[team]) is None
    assert resolver["calls"] == []


def test_no_database_in_the_question_means_no_team_waiver(resolver):
    assert not auto_approve._team_waiver_applies(UID, 53, None)
    assert not auto_approve._team_waiver_applies(UID, None, "ledger")
    assert resolver["calls"] == []


# --- the read ----------------------------------------------------------------

def test_the_read_adds_v2_only_waivers_under_the_new_model(monkeypatch):
    sql = []
    monkeypatch.setattr(auto_approve.db, "fetch_all",
                        lambda q, p=None: sql.append(q) or [])
    monkeypatch.setattr(teams, "use_v2", lambda: True)
    auto_approve.active_grants(UID, NOW)
    assert any("FROM auto_approve_grants" in q for q in sql)
    assert any("FROM access_grant" in q for q in sql)


def test_the_old_model_reads_only_the_legacy_table(monkeypatch):
    sql = []
    monkeypatch.setattr(auto_approve.db, "fetch_all",
                        lambda q, p=None: sql.append(q) or [])
    monkeypatch.setattr(teams, "use_v2", lambda: False)
    auto_approve.active_grants(UID, NOW)
    assert not any("FROM access_grant" in q for q in sql)


def test_mirrored_rows_are_not_read_twice():
    """A mirrored row is a copy of a legacy row the first query already
    returned; reading it again would give one grant two ids."""
    src = inspect.getsource(auto_approve._v2_only_waivers)
    assert "g.mirrored_from IS NULL" in src
    assert "g.auto_approve" in src
    assert "g.revoked_at IS NULL AND NOT g.is_deleted" in src
    assert "NOT tm.is_deleted" in src


def test_v2_ids_cannot_be_mistaken_for_legacy_ids():
    assert "'ag:' || g.id AS id" in inspect.getsource(auto_approve._v2_only_waivers)


def test_the_rule_is_asked_of_the_access_model_not_rewritten_here():
    src = inspect.getsource(auto_approve._team_waiver_applies)
    assert "access.team_waivers_reach(" in src
    assert "merge_with_team" not in src


# --- the label ---------------------------------------------------------------

def test_the_decision_label_names_the_team():
    label = auto_approve.decided_by_name_for(_row("ag:461", team_id=14, team_name="Team Alpha"))
    assert label.startswith("auto-approved (team Team Alpha waiver ag:461")


def test_a_personal_label_is_unchanged():
    assert auto_approve.decided_by_name_for(_row(70)) == \
        "auto-approved (grant #70, max_tier=ro, no expiry)"


# --- the rule, in the access model -------------------------------------------

def _g(mine, auto=False, merge=False, tier="rw", rank=2):
    return {"mine": mine, "tier": tier, "rank": rank, "auto_approve": auto,
            "merge_with_team": merge, "all_targets": False, "all_databases": False,
            "database_name": "ledger", "db_role": None, "expired": False,
            "not_started": False}


@pytest.fixture
def covering(monkeypatch):
    state = {"rows": [], "admin": False}
    monkeypatch.setattr(access, "_covering", lambda pid, tid, dbn: state["rows"])
    monkeypatch.setattr(access, "is_admin", lambda pid: state["admin"])
    return state


def test_a_team_waiver_reaches_a_member_who_has_only_team_access(covering):
    covering["rows"] = [_g(False), _g(False, auto=True, tier="ro", rank=1)]
    assert access.team_waivers_reach(UID, 53, "ledger")


def test_the_members_own_grant_displaces_the_team_waiver(covering):
    """Rule 4. Their own waiver is irrelevant to this question -- it is exactly
    what made the broader `auto_tier` question the wrong one to ask."""
    covering["rows"] = [_g(True), _g(True, auto=True, tier="ro", rank=1),
                        _g(False, auto=True, tier="ro", rank=1)]
    assert not access.team_waivers_reach(UID, 53, "ledger")


def test_an_own_grant_that_merges_with_the_team_does_not_displace_it(covering):
    covering["rows"] = [_g(True, merge=True), _g(False, auto=True, tier="ro", rank=1)]
    assert access.team_waivers_reach(UID, 53, "ledger")


def test_no_access_means_no_team_waiver(covering):
    covering["rows"] = [_g(False, auto=True, tier="ro", rank=1)]
    assert not access.team_waivers_reach(UID, 53, "ledger")


def test_a_team_waiver_reaches_an_admin(covering):
    covering["admin"] = True
    assert access.team_waivers_reach(UID, 53, "ledger")


def test_the_access_decision_and_the_waiver_question_share_one_rule():
    assert "_suppresses_team(" in inspect.getsource(access._decide)
    assert "_suppresses_team(" in inspect.getsource(access.team_waivers_reach)
    assert "merge_with_team" not in inspect.getsource(access.team_waivers_reach)
